from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repoctl.scanner.util import make_repository_id
from repoctl.web.app import create_web_app
from repoctl.workflow.git_state import WorkflowGitStateError
from repoctl.workflow.lifecycle import record_milestone_event
from repoctl.workflow.milestones import save_milestone_selection

DOCUMENTS = {
    "context": "docs/context/project_context.md",
    "architecture": "docs/context/project_architecture.md",
    "workflow": "docs/context/project_workflow.md",
    "coding_agent_rules": "docs/context/coding_agent_rules.md",
    "parking_lot": "docs/context/canonical_parking_lot.md",
}
MILESTONE_ID = "M003.004"
PROMPT_PATH = f"docs/milestones/{MILESTONE_ID}_architect_read_only_repo_control_contract_prompt.md"
CLOSEOUT_PATH = f"docs/milestones/{MILESTONE_ID}_architect_read_only_repo_control_contract_closeout.md"


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _repo(root: Path, name: str, marker: str) -> Path:
    repository = root / name
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test User")
    (repository / "tracked.txt").write_text("tracked content\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-qm", "initial")
    prompt = repository / PROMPT_PATH
    prompt.parent.mkdir(parents=True)
    prompt.write_text(f"# Prompt\n\n{marker} prompt content.\n", encoding="utf-8")
    (repository / CLOSEOUT_PATH).write_text("# Closeout\n\nAccepted evidence.\n", encoding="utf-8")
    for role, relative_path in DOCUMENTS.items():
        document = repository / relative_path
        document.parent.mkdir(parents=True, exist_ok=True)
        document.write_text(f"# {role}\n\n{marker} {role} content.\n", encoding="utf-8")
    return repository


def _registry(root: Path, repositories: list[tuple[str, str, Path]], default: str) -> Path:
    lines = [f"default = {json.dumps(default)}", ""]
    for key, name, repository in repositories:
        lines.extend(
            [
                f"[repositories.{key}]",
                f"name = {json.dumps(name)}",
                f"path = {json.dumps(str(repository))}",
                'milestone_root = "docs/milestones"',
                "",
                f"[repositories.{key}.documents]",
            ]
        )
        lines.extend(f"{role} = {json.dumps(path)}" for role, path in DOCUMENTS.items())
        lines.append("")
    path = root / "repositories.toml"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _select(repository: Path, state_root: Path) -> dict:
    return save_milestone_selection(repository, state_root, MILESTONE_ID, "docs/milestones")


def _record(repository: Path, state_root: Path, event_type: str, summary: str, **kwargs) -> dict:
    selection = _select(repository, state_root)
    return record_milestone_event(
        repository,
        state_root,
        "docs/milestones",
        selection,
        event_type=event_type,
        summary=summary,
        **kwargs,
    )


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.parts
    }


class ArchitectContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.first = _repo(self.root, "first", "FIRST_ONLY")
        self.second = _repo(self.root, "second", "SECOND_ONLY")
        self.state_root = self.root / "state"
        _select(self.first, self.state_root)
        _select(self.second, self.state_root)
        self.registry = _registry(
            self.root,
            [
                ("first", "First Repository", self.first),
                ("second", "Second Repository", self.second),
            ],
            "first",
        )
        self.app = create_web_app(repository_registry_path=self.registry, state_root=self.state_root)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _historical_fixtures(self) -> None:
        milestone_root = self.first / "docs/milestones"
        milestone_root.mkdir(parents=True, exist_ok=True)
        slug = "prior_searchable_decision"
        for suffix, body in (
            ("prompt", "# Prior Prompt\n\nlegacy_signal search phrase\n"),
            ("workflow", "# Prior Workflow\n\nworkflow audit history\n"),
            ("closeout", "# Prior Closeout\n\nlegacy_signal accepted result\n"),
        ):
            (milestone_root / f"M003.003_{slug}_{suffix}.md").write_text(
                body,
                encoding="utf-8",
            )
        history_context = self.first / "docs/history/context"
        history_context.mkdir(parents=True)
        (history_context / "project_context_v1.md").write_text(
            "# Archived Context\n\nlegacy_signal archived context evidence\n",
            encoding="utf-8",
        )
        history_milestones = self.first / "docs/history/milestones"
        history_milestones.mkdir(parents=True)
        (history_milestones / "002.1_prior_design_closeout.md").write_text(
            "# Archived Planning Closeout\n\nrejected_design archive evidence\n",
            encoding="utf-8",
        )

    def _open_descriptor_count(self) -> int:
        descriptor_root = Path("/proc/self/fd")
        if not descriptor_root.is_dir():
            self.skipTest("open-file descriptor measurements require /proc/self/fd")
        return len(os.listdir(descriptor_root))

    def test_repeated_nested_historical_search_and_read_keep_descriptors_stable(self) -> None:
        self._historical_fixtures()
        history_milestones = self.first / "docs/history/application/milestones"
        history_milestones.mkdir(parents=True)
        history_files = {
            "docs/history/application/milestones/M001.001_oldest_prompt.md": (
                "# Oldest milestone\n\napplication_history_marker\n"
            ),
            "docs/history/application/milestones/M001.002_intermediate_workflow.md": (
                "# Intermediate milestone\n\napplication_history_marker\n"
            ),
            "docs/history/application/milestones/M001.003_newest_closeout.md": (
                "# Newest milestone\n\napplication_history_marker\n"
            ),
        }
        for relative_path, content in history_files.items():
            path = self.first / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        deployment_path = "docs/history/deployments/procedure_release.md"
        deployment = self.first / deployment_path
        deployment.parent.mkdir(parents=True)
        deployment.write_text(
            "# Eligible deployment evidence\n\napplication_history_marker deployment_evidence\n",
            encoding="utf-8",
        )

        search_path = "/api/architect/v1/history/search"
        search_args = {
            "query": "application_history_marker",
            "expected_repository_key": "first",
            "expected_generation": "0",
            "limit": "10",
        }
        initial = self.client.get(search_path, query_string=search_args)
        self.assertEqual(initial.status_code, 200, initial.get_json())
        initial_results = initial.get_json()["data"]["results"]
        paths = {item["path"] for item in initial_results}
        self.assertTrue(set(history_files).issubset(paths))
        deployment_result = next(item for item in initial_results if item["path"] == deployment_path)
        self.assertEqual(deployment_result["artifact_type"], "historical_document")
        self.assertEqual(deployment_result["category"], "archive")

        read_path = "/api/architect/v1/history/document"
        operation_count = 25
        tolerance = 2
        baseline = self._open_descriptor_count()
        for index in range(operation_count):
            search = self.client.get(search_path, query_string=search_args)
            self.assertEqual(search.status_code, 200, search.get_json())
            results = search.get_json()["data"]["results"]
            result_path = sorted(history_files)[index % len(history_files)]
            self.assertIn(result_path, {item["path"] for item in results})
            read = self.client.get(
                read_path,
                query_string={
                    "path": result_path if index % 2 == 0 else deployment_path,
                    "expected_repository_key": "first",
                    "expected_generation": "0",
                },
            )
            self.assertEqual(read.status_code, 200, read.get_json())
            self.assertEqual(read.get_json()["data"]["source_type"], "historical_evidence")

        after = self._open_descriptor_count()
        self.assertLessEqual(
            abs(after - baseline),
            tolerance,
            f"{operation_count} nested search/read cycles changed open FDs from {baseline} to {after}",
        )

    def test_historical_scan_failure_closes_open_descriptors(self) -> None:
        self._historical_fixtures()
        query = {
            "query": "legacy_signal",
            "expected_repository_key": "first",
            "expected_generation": "0",
        }
        operation_count = 10
        tolerance = 2
        baseline = self._open_descriptor_count()
        original_scandir = os.scandir

        def fail_descriptor_scan(path):
            if isinstance(path, int):
                raise OSError("injected scan failure")
            return original_scandir(path)

        with patch("repoctl.web.architect_contract.os.scandir", side_effect=fail_descriptor_scan):
            for _ in range(operation_count):
                response = self.client.get("/api/architect/v1/history/search", query_string=query)
                self.assertEqual(response.status_code, 503, response.get_json())
                self.assertEqual(
                    response.get_json()["error"]["code"],
                    "historical_discovery_failed",
                )
        after = self._open_descriptor_count()
        self.assertLessEqual(
            abs(after - baseline),
            tolerance,
            f"{operation_count} failed scans changed open FDs from {baseline} to {after}",
        )

    def test_historical_entry_limit_short_circuit_closes_open_descriptors(self) -> None:
        self._historical_fixtures()
        query = {
            "query": "legacy_signal",
            "expected_repository_key": "first",
            "expected_generation": "0",
        }
        operation_count = 10
        tolerance = 2
        baseline = self._open_descriptor_count()
        with patch("repoctl.web.architect_contract.MAX_HISTORY_DIRECTORY_ENTRIES", 1):
            for _ in range(operation_count):
                response = self.client.get("/api/architect/v1/history/search", query_string=query)
                self.assertEqual(response.status_code, 503, response.get_json())
                self.assertEqual(
                    response.get_json()["error"]["code"],
                    "historical_discovery_incomplete",
                )
        after = self._open_descriptor_count()
        self.assertLessEqual(
            abs(after - baseline),
            tolerance,
            f"{operation_count} limited scans changed open FDs from {baseline} to {after}",
        )

    def test_historical_search_is_bounded_deterministic_and_excludes_active_milestone(self) -> None:
        self._historical_fixtures()
        path = "/api/architect/v1/history/search"
        query = {
            "query": "legacy_signal",
            "expected_repository_key": "first",
            "expected_generation": "0",
            "limit": "2",
        }
        first = self.client.get(path, query_string=query)
        second = self.client.get(path, query_string=query)
        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(first.get_json(), second.get_json())
        data = first.get_json()["data"]
        self.assertEqual(data["total_count"], 2)
        self.assertEqual(data["returned_count"], 2)
        self.assertEqual(data["omitted_count"], 0)
        self.assertTrue(data["truncated"])
        self.assertTrue(data["has_more"])
        self.assertIsInstance(data["next_cursor"], str)
        self.assertFalse(data["total_count_complete"])
        self.assertEqual(
            [item["path"] for item in data["results"]],
            [
                "docs/history/context/project_context_v1.md",
                "docs/milestones/M003.003_prior_searchable_decision_closeout.md",
            ],
        )
        for item in data["results"]:
            self.assertEqual(item["repository_key"], "first")
            self.assertEqual(item["repository_id"], make_repository_id(self.first))
            self.assertEqual(item["source_type"], "historical_evidence")
            self.assertEqual(item["authority_classification"], "historical_evidence")
            self.assertIn("not current governing or lifecycle authority", item["authority_note"])
            self.assertFalse(item["is_current_authority"])
            self.assertEqual(item["working_tree_source"], "current_working_tree")
            self.assertGreater(item["file_size_bytes"], 0)
            self.assertNotIn("content", item)
            self.assertIn("match_metadata", item)

        next_page = self.client.get(
            path,
            query_string={**query, "cursor": data["next_cursor"]},
        )
        self.assertEqual(next_page.status_code, 200, next_page.get_json())
        next_data = next_page.get_json()["data"]
        self.assertEqual(
            [item["path"] for item in next_data["results"]],
            ["docs/milestones/M003.003_prior_searchable_decision_prompt.md"],
        )
        self.assertEqual(next_data["total_count"], 3)
        self.assertFalse(next_data["has_more"])
        self.assertIsNone(next_data["next_cursor"])
        self.assertTrue(next_data["total_count_complete"])

        selected_milestone = self.client.get(
            path,
            query_string={
                "milestone_id": MILESTONE_ID,
                "expected_repository_key": "first",
                "expected_generation": "0",
            },
        )
        self.assertEqual(selected_milestone.status_code, 200)
        self.assertEqual(selected_milestone.get_json()["data"]["total_count"], 0)

        no_query_inventory = self.client.get(
            path,
            query_string={
                "category": "milestone",
                "artifact_type": "milestone_prompt",
                "expected_repository_key": "first",
                "expected_generation": "0",
            },
        )
        self.assertEqual(no_query_inventory.status_code, 200, no_query_inventory.get_json())
        inventory_paths = [item["path"] for item in no_query_inventory.get_json()["data"]["results"]]
        self.assertEqual(inventory_paths, ["docs/milestones/M003.003_prior_searchable_decision_prompt.md"])

        workflow_filter = self.client.get(
            path,
            query_string={
                "milestone_id": "M003.003",
                "artifact_type": "milestone_workflow",
                "category": "milestone",
                "expected_repository_key": "first",
                "expected_generation": "0",
            },
        )
        self.assertEqual(workflow_filter.status_code, 200, workflow_filter.get_json())
        self.assertEqual(
            [item["path"] for item in workflow_filter.get_json()["data"]["results"]],
            ["docs/milestones/M003.003_prior_searchable_decision_workflow.md"],
        )

        zero_match = self.client.get(
            path,
            query_string={
                "query": "no_such_historical_marker",
                "expected_repository_key": "first",
                "expected_generation": "0",
            },
        )
        self.assertEqual(zero_match.status_code, 200)
        self.assertEqual(zero_match.get_json()["data"]["total_count"], 0)
        self.assertEqual(zero_match.get_json()["data"]["returned_count"], 0)

        with patch("repoctl.web.architect_contract.MAX_HISTORY_SEARCH_FILES", 1):
            bounded_scan = self.client.get(path, query_string=query)
        self.assertEqual(bounded_scan.status_code, 200, bounded_scan.get_json())
        self.assertTrue(bounded_scan.get_json()["data"]["limited"])
        self.assertFalse(bounded_scan.get_json()["data"]["total_count_complete"])
        self.assertEqual(bounded_scan.get_json()["data"]["files_scanned"], 1)
        self.assertTrue(bounded_scan.get_json()["data"]["has_more"])
        self.assertIsInstance(bounded_scan.get_json()["data"]["next_cursor"], str)

        with patch("repoctl.web.architect_contract.MAX_HISTORY_CANDIDATES", 2):
            bounded_candidates = self.client.get(path, query_string=query)
        self.assertEqual(bounded_candidates.status_code, 503, bounded_candidates.get_json())
        self.assertEqual(
            bounded_candidates.get_json()["error"]["code"],
            "historical_discovery_incomplete",
        )

        broad_inventory = self.client.get(
            path,
            query_string={
                "expected_repository_key": "first",
                "expected_generation": "0",
            },
        )
        self.assertEqual(broad_inventory.status_code, 400)
        self.assertEqual(
            broad_inventory.get_json()["error"]["code"],
            "historical_inventory_too_broad",
        )

        archive_inventory = self.client.get(
            path,
            query_string={
                "milestone_id": "002.1",
                "expected_repository_key": "first",
                "expected_generation": "0",
            },
        )
        self.assertEqual(archive_inventory.status_code, 200)
        result = archive_inventory.get_json()["data"]["results"][0]
        self.assertEqual(result["path"], "docs/history/milestones/002.1_prior_design_closeout.md")
        self.assertEqual(result["artifact_type"], "milestone_closeout")

        repo_document = self.client.get(
            "/api/architect/v1/repository-document",
            query_string={
                "path": "docs/history/context/project_context_v1.md",
                "expected_repository_key": "first",
                "expected_generation": "0",
            },
        )
        self.assertEqual(repo_document.status_code, 403)
        self.assertEqual(
            repo_document.get_json()["error"]["code"],
            "historical_document_not_supported",
        )

    def test_historical_search_continues_past_300_and_inventory_pages_without_gaps(self) -> None:
        history_root = self.first / "docs/history/deployments"
        history_root.mkdir(parents=True)
        candidate_paths = []
        for index in range(1_005):
            relative_path = f"docs/history/deployments/archive_{index:04d}.md"
            (self.first / relative_path).write_text("ordinary deployment notes\n", encoding="utf-8")
            candidate_paths.append(relative_path)
        target_path = "docs/history/deployments/zzzz_late_fstab_sanitized.md"
        (self.first / target_path).write_text(
            "# Deployment evidence\n\nfstab sanitized deployment evidence\n",
            encoding="utf-8",
        )
        candidate_paths.append(target_path)

        search_path = "/api/architect/v1/history/search"
        search_args = {
            "query": "fstab sanitized",
            "expected_repository_key": "first",
            "expected_generation": "0",
            "limit": "10",
        }
        found = []
        cursor = None
        page_count = 0
        while True:
            page_args = dict(search_args)
            if cursor is not None:
                page_args["cursor"] = cursor
            response = self.client.get(search_path, query_string=page_args)
            self.assertEqual(response.status_code, 200, response.get_json())
            data = response.get_json()["data"]
            self.assertEqual(data["candidates_considered"], 1_006)
            self.assertLessEqual(data["files_scanned"], 300)
            found.extend(item["path"] for item in data["results"])
            page_count += 1
            if not data["has_more"]:
                self.assertTrue(data["total_count_complete"])
                break
            self.assertIsInstance(data["next_cursor"], str)
            self.assertFalse(data["total_count_complete"])
            cursor = data["next_cursor"]
        self.assertEqual(page_count, 4)
        self.assertEqual(found, [target_path])
        self.assertEqual(data["total_count"], 1)

        read = self.client.get(
            "/api/architect/v1/history/document",
            query_string={
                "path": found[0],
                "expected_repository_key": "first",
                "expected_generation": "0",
            },
        )
        self.assertEqual(read.status_code, 200, read.get_json())
        self.assertIn("fstab sanitized", read.get_json()["data"]["content"])

        inventory_args = {
            "artifact_type": "historical_document",
            "category": "archive",
            "expected_repository_key": "first",
            "expected_generation": "0",
            "limit": "20",
        }
        inventory_paths = []
        cursor = None
        while True:
            page_args = dict(inventory_args)
            if cursor is not None:
                page_args["cursor"] = cursor
            response = self.client.get(search_path, query_string=page_args)
            self.assertEqual(response.status_code, 200, response.get_json())
            data = response.get_json()["data"]
            inventory_paths.extend(item["path"] for item in data["results"])
            self.assertTrue(data["total_count_complete"])
            if not data["has_more"]:
                break
            cursor = data["next_cursor"]
        self.assertEqual(inventory_paths, sorted(candidate_paths))
        self.assertEqual(len(inventory_paths), len(set(inventory_paths)))
        self.assertEqual(len(inventory_paths), 1_006)

    def test_historical_search_reads_beyond_document_return_limit(self) -> None:
        history_root = self.first / "docs/history/deployments"
        history_root.mkdir(parents=True)
        target_path = "docs/history/deployments/long_document.md"
        marker = "late_document_search_marker"
        content = "x" * (128_000 + 32) + marker
        (self.first / target_path).write_text(content, encoding="utf-8")

        response = self.client.get(
            "/api/architect/v1/history/search",
            query_string={
                "query": marker,
                "expected_repository_key": "first",
                "expected_generation": "0",
            },
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        data = response.get_json()["data"]
        self.assertEqual([item["path"] for item in data["results"]], [target_path])
        self.assertEqual(data["bytes_scanned"], len(content.encode("utf-8")))
        self.assertTrue(data["total_count_complete"])

    def test_historical_search_byte_budget_continuation_does_not_skip_candidates(self) -> None:
        history_root = self.first / "docs/history/deployments"
        history_root.mkdir(parents=True)
        paths = []
        for index in range(3):
            relative_path = f"docs/history/deployments/budget_{index}.md"
            (self.first / relative_path).write_text(
                f"budget_marker document {index}\n",
                encoding="utf-8",
            )
            paths.append(relative_path)

        search_path = "/api/architect/v1/history/search"
        args = {
            "query": "budget_marker",
            "expected_repository_key": "first",
            "expected_generation": "0",
            "limit": "10",
        }
        found = []
        cursor = None
        with patch("repoctl.web.architect_contract.MAX_HISTORY_SEARCH_BYTES", 30):
            while True:
                page_args = dict(args)
                if cursor is not None:
                    page_args["cursor"] = cursor
                response = self.client.get(search_path, query_string=page_args)
                self.assertEqual(response.status_code, 200, response.get_json())
                data = response.get_json()["data"]
                self.assertLessEqual(data["bytes_scanned"], 30)
                found.extend(item["path"] for item in data["results"])
                if not data["has_more"]:
                    self.assertTrue(data["total_count_complete"])
                    break
                self.assertTrue(data["limited"])
                cursor = data["next_cursor"]
        self.assertEqual(found, sorted(paths))

    def test_historical_search_cursor_rejects_mismatch_and_changed_inventory(self) -> None:
        self._historical_fixtures()
        search_path = "/api/architect/v1/history/search"
        args = {
            "query": "legacy_signal",
            "expected_repository_key": "first",
            "expected_generation": "0",
            "limit": "1",
        }
        first = self.client.get(search_path, query_string=args)
        self.assertEqual(first.status_code, 200, first.get_json())
        cursor = first.get_json()["data"]["next_cursor"]
        self.assertIsInstance(cursor, str)

        mismatched = self.client.get(search_path, query_string={**args, "query": "other", "cursor": cursor})
        self.assertEqual(mismatched.status_code, 400)
        self.assertEqual(mismatched.get_json()["error"]["code"], "invalid_history_cursor")

        malformed = self.client.get(search_path, query_string={**args, "cursor": "not-a-cursor"})
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(malformed.get_json()["error"]["code"], "invalid_history_cursor")

        tampered_cursor = cursor[:12] + ("A" if cursor[12] != "A" else "B") + cursor[13:]
        tampered = self.client.get(search_path, query_string={**args, "cursor": tampered_cursor})
        self.assertEqual(tampered.status_code, 400)
        self.assertEqual(tampered.get_json()["error"]["code"], "invalid_history_cursor")

        (self.first / "docs/history/context/project_context_v1.md").write_text(
            "# Changed after page one\n\nlegacy_signal updated\n",
            encoding="utf-8",
        )
        stale = self.client.get(search_path, query_string={**args, "cursor": cursor})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.get_json()["error"]["code"], "stale_history_cursor")

    def test_historical_document_read_policy_and_provenance(self) -> None:
        self._historical_fixtures()
        path = "/api/architect/v1/history/document"
        valid_args = {
            "expected_repository_key": "first",
            "expected_generation": "0",
        }
        for relative_path, expected_type in (
            (
                "docs/milestones/M003.003_prior_searchable_decision_prompt.md",
                "milestone_prompt",
            ),
            (
                "docs/milestones/M003.003_prior_searchable_decision_closeout.md",
                "milestone_closeout",
            ),
            ("docs/history/context/project_context_v1.md", "archived_context"),
        ):
            response = self.client.get(
                path,
                query_string={**valid_args, "path": relative_path},
            )
            self.assertEqual(response.status_code, 200, response.get_json())
            data = response.get_json()["data"]
            self.assertEqual(data["path"], relative_path)
            self.assertEqual(data["artifact_type"], expected_type)
            self.assertEqual(data["source_type"], "historical_evidence")
            self.assertEqual(data["repository_key"], "first")
            self.assertEqual(data["repository_id"], make_repository_id(self.first))
            self.assertEqual(data["authority_classification"], "historical_evidence")
            self.assertIn("not current governing or lifecycle authority", data["authority_note"])
            self.assertFalse(data["is_current_authority"])
            self.assertEqual(data["working_tree_source"], "current_working_tree")
            self.assertEqual(data["file_size_bytes"], data["bytes_returned"])
            self.assertFalse(data["truncated"])
            self.assertTrue(data["content"])

        invalid_cases = [
            ("../README.md", "invalid_history_path", 400),
            ("/etc/passwd", "invalid_history_path", 400),
            ("src/secret.md", "historical_path_not_allowed", 403),
            ("docs/history/context/unsupported.pdf", "unsupported_historical_document_type", 415),
            (
                "docs/milestones/M003.004_architect_read_only_repo_control_contract_prompt.md",
                "current_artifact_not_historical",
                409,
            ),
            ("docs/history/context/missing.md", "historical_artifact_missing", 404),
        ]
        for relative_path, code, status in invalid_cases:
            with self.subTest(path=relative_path):
                response = self.client.get(path, query_string={**valid_args, "path": relative_path})
                self.assertEqual(response.status_code, status, response.get_json())
                self.assertEqual(response.get_json()["error"]["code"], code)

        secret = self.first / "docs/history/context/secrets/notes.md"
        secret.parent.mkdir()
        secret.write_text("not an allowed document\n", encoding="utf-8")
        denied_secret = self.client.get(
            path,
            query_string={**valid_args, "path": "docs/history/context/secrets/notes.md"},
        )
        self.assertEqual(denied_secret.status_code, 403)
        self.assertEqual(denied_secret.get_json()["error"]["code"], "historical_path_not_allowed")

        symlink = self.first / "docs/history/context/symlink.md"
        symlink.symlink_to(self.second / "docs/context/project_context.md")
        denied_symlink = self.client.get(path, query_string={**valid_args, "path": "docs/history/context/symlink.md"})
        self.assertEqual(denied_symlink.status_code, 422)
        self.assertEqual(denied_symlink.get_json()["error"]["code"], "unsafe_historical_path")

        invalid_encoding = self.first / "docs/history/context/invalid.md"
        invalid_encoding.write_bytes(b"\xff\xfe")
        denied_encoding = self.client.get(path, query_string={**valid_args, "path": "docs/history/context/invalid.md"})
        self.assertEqual(denied_encoding.status_code, 422)
        self.assertEqual(denied_encoding.get_json()["error"]["code"], "invalid_historical_encoding")

        oversized = self.first / "docs/history/context/oversized.md"
        oversized.write_bytes(b"a" * 128_001)
        truncated = self.client.get(path, query_string={**valid_args, "path": "docs/history/context/oversized.md"})
        self.assertEqual(truncated.status_code, 200, truncated.get_json())
        self.assertTrue(truncated.get_json()["data"]["truncated"])
        self.assertEqual(truncated.get_json()["data"]["file_size_bytes"], 128_001)
        self.assertEqual(truncated.get_json()["data"]["bytes_returned"], 128_000)
        too_large = self.first / "docs/history/context/too-large.md"
        with too_large.open("wb") as stream:
            stream.truncate(5_000_001)
        rejected_size = self.client.get(path, query_string={**valid_args, "path": "docs/history/context/too-large.md"})
        self.assertEqual(rejected_size.status_code, 413)
        self.assertEqual(rejected_size.get_json()["error"]["code"], "historical_artifact_oversized")

        directory = self.first / "docs/history/context/directory.md"
        directory.mkdir()
        rejected_directory = self.client.get(path, query_string={**valid_args, "path": "docs/history/context/directory.md"})
        self.assertEqual(rejected_directory.status_code, 422)
        self.assertEqual(rejected_directory.get_json()["error"]["code"], "unsafe_historical_path")

    def test_history_search_and_read_enforce_repository_preconditions(self) -> None:
        self._historical_fixtures()
        search = self.client.get(
            "/api/architect/v1/history/search",
            query_string={
                "query": "legacy_signal",
                "expected_repository_key": "second",
                "expected_generation": "0",
            },
        )
        self.assertEqual(search.status_code, 409)
        self.assertEqual(search.get_json()["error"]["code"], "stale_repository_selection")

        stale = self.client.get(
            "/api/architect/v1/history/search",
            query_string={
                "query": "legacy_signal",
                "expected_repository_key": "first",
                "expected_generation": "1",
            },
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.get_json()["error"]["code"], "stale_repository_generation")

        invalid_query = self.client.get(
            "/api/architect/v1/history/search",
            query_string={
                "query": "!!!",
                "expected_repository_key": "first",
                "expected_generation": "0",
            },
        )
        self.assertEqual(invalid_query.status_code, 400)
        self.assertEqual(invalid_query.get_json()["error"]["code"], "invalid_history_query")

        invalid_type = self.client.get(
            "/api/architect/v1/history/search",
            query_string={
                "query": "legacy_signal",
                "artifact_type": "source_code",
                "expected_repository_key": "first",
                "expected_generation": "0",
            },
        )
        self.assertEqual(invalid_type.status_code, 400)
        self.assertEqual(
            invalid_type.get_json()["error"]["code"],
            "unsupported_historical_artifact_type",
        )

        invalid_limit = self.client.get(
            "/api/architect/v1/history/search",
            query_string={
                "query": "legacy_signal",
                "limit": "21",
                "expected_repository_key": "first",
                "expected_generation": "0",
            },
        )
        self.assertEqual(invalid_limit.status_code, 400)
        self.assertEqual(invalid_limit.get_json()["error"]["code"], "invalid_history_limit")

    def test_history_is_available_without_selection_and_without_archive_root(self) -> None:
        self._historical_fixtures()
        selection = self.state_root / make_repository_id(self.first) / "milestones" / "selection.json"
        selection.unlink()
        no_selection = self.client.get(
            "/api/architect/v1/history/search",
            query_string={
                "query": "legacy_signal",
                "expected_repository_key": "first",
                "expected_generation": "0",
            },
        )
        self.assertEqual(no_selection.status_code, 200, no_selection.get_json())
        self.assertGreater(no_selection.get_json()["data"]["total_count"], 0)

        second_registry_root = self.root / "second-registry"
        second_registry_root.mkdir()
        second_registry = _registry(
            second_registry_root,
            [("second", "Second Repository", self.second)],
            "second",
        )
        second_client = create_web_app(
            repository_registry_path=second_registry,
            state_root=self.state_root,
        ).test_client()
        no_archive = second_client.get(
            "/api/architect/v1/history/search",
            query_string={
                "category": "archive",
                "artifact_type": "archived_context",
                "expected_repository_key": "second",
                "expected_generation": "0",
            },
        )
        self.assertEqual(no_archive.status_code, 200, no_archive.get_json())
        self.assertEqual(no_archive.get_json()["data"]["total_count"], 0)
        isolated = second_client.get(
            "/api/architect/v1/history/search",
            query_string={
                "query": "FIRST_ONLY",
                "expected_repository_key": "second",
                "expected_generation": "0",
            },
        )
        self.assertEqual(isolated.status_code, 200, isolated.get_json())
        self.assertEqual(isolated.get_json()["data"]["total_count"], 0)

    def test_historical_reads_do_not_mutate_selection_or_git_state(self) -> None:
        self._historical_fixtures()
        selection = self.state_root / make_repository_id(self.first) / "milestones" / "selection.json"
        selection_before = selection.read_bytes()
        head_before = _git(self.first, "rev-parse", "HEAD")
        status_before = _git(self.first, "status", "--porcelain")
        query = self.client.get(
            "/api/architect/v1/history/search",
            query_string={
                "query": "legacy_signal",
                "expected_repository_key": "first",
                "expected_generation": "0",
            },
        )
        self.assertEqual(query.status_code, 200, query.get_json())
        read = self.client.get(
            "/api/architect/v1/history/document",
            query_string={
                "path": "docs/history/context/project_context_v1.md",
                "expected_repository_key": "first",
                "expected_generation": "0",
            },
        )
        self.assertEqual(read.status_code, 200, read.get_json())
        self.assertEqual(selection.read_bytes(), selection_before)
        self.assertEqual(_git(self.first, "rev-parse", "HEAD"), head_before)
        self.assertEqual(_git(self.first, "status", "--porcelain"), status_before)

    def test_history_discovery_rejects_symlinked_archive_root(self) -> None:
        history_root = self.first / "docs/history"
        history_root.parent.mkdir(parents=True, exist_ok=True)
        history_root.symlink_to(self.second / "docs", target_is_directory=True)
        response = self.client.get(
            "/api/architect/v1/history/search",
            query_string={
                "query": "anything",
                "expected_repository_key": "first",
                "expected_generation": "0",
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.get_json()["error"]["code"], "unsafe_historical_root")

    def test_status_prompt_workflow_closeout_and_review_contract(self) -> None:
        _record(
            self.first,
            self.state_root,
            "prompt_approved",
            "Prompt approved.",
            actor_role="product_owner",
            po_approved=True,
        )
        _record(self.first, self.state_root, "implementation_started", "Implementation started.", actor_role="coder")
        _record(
            self.first,
            self.state_root,
            "lock_in_approved",
            "Keep the response provider-neutral.",
            actor_role="product_owner",
            po_approved=True,
        )
        _record(self.first, self.state_root, "decision_recorded", "Consider pagination later.", actor_role="architect")
        _record(
            self.first,
            self.state_root,
            "question_submitted",
            "How should stale generations be handled?",
            actor_role="architect",
        )
        _record(
            self.first,
            self.state_root,
            "escalation_opened",
            "External authentication remains out of scope.",
            actor_role="coder",
            waiting_category="escalation",
            waiting_reason="Requires a future milestone.",
        )
        _record(
            self.first,
            self.state_root,
            "test_result_recorded",
            "Focused tests passed.",
            actor_role="coder",
            evidence_source="pytest focused suite",
            result="PASS, reported",
        )

        status = self.client.get("/api/architect/v1/status")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.mimetype, "application/json")
        status_body = status.get_json()
        self.assertEqual(status_body["contract_version"], "1")
        self.assertEqual(status_body["repository"]["key"], "first")
        self.assertEqual(status_body["repository"]["name"], "First Repository")
        self.assertEqual(status_body["repository"]["registry_generation"], 0)
        self.assertEqual(status_body["selection"]["milestone_id"], MILESTONE_ID)
        self.assertEqual(status_body["data"]["milestone"]["phase"], "WAITING")
        self.assertEqual(status_body["data"]["milestone"]["waiting_reason"], "Requires a future milestone.")
        self.assertEqual(status_body["data"]["milestone"]["closeout"]["status"], "present")
        self.assertEqual(status_body["data"]["milestone"]["workflow"]["status"], "present")
        runtime = status_body["data"]["runtime_capabilities"]
        self.assertEqual(runtime["capability_schema_version"], 1)
        self.assertTrue(runtime["instance_id"])
        self.assertEqual(runtime["workflow"]["ledger_schema_version"], 1)
        self.assertEqual(runtime["workflow"]["context_verification_record_schema_version"], 2)

        prompt = self.client.get("/api/architect/v1/prompt").get_json()["data"]
        self.assertEqual(prompt["source_type"], "milestone_prompt")
        self.assertEqual(prompt["source_path"], PROMPT_PATH)
        self.assertFalse(prompt["truncated"])
        self.assertIn("FIRST_ONLY prompt content.", prompt["content"])

        workflow = self.client.get("/api/architect/v1/workflow").get_json()["data"]
        self.assertEqual(workflow["phase"], "WAITING")
        self.assertEqual(len(workflow["binding_lock_ins"]["events"]), 1)
        self.assertTrue(workflow["binding_lock_ins"]["events"][0]["binding"])
        self.assertTrue(all(not event["actor_authenticated"] for event in workflow["events"]["events"]))
        decisions = [
            event for event in workflow["events"]["events"]
            if event["event_type"] == "decision_recorded"
        ]
        self.assertEqual(len(decisions), 1)
        self.assertFalse(decisions[0]["binding"])
        self.assertEqual(
            {event["event_type"] for event in workflow["open_conditions"]["events"]},
            {"question_submitted", "escalation_opened"},
        )

        evidence = self.client.get("/api/architect/v1/evidence").get_json()["data"]
        self.assertEqual(evidence["reported_vs_machine_observed"], "reported")
        self.assertEqual(evidence["events"][0]["evidence_status"], "reported")
        self.assertEqual(evidence["events"][0]["result"], "PASS, reported")

        closeout = self.client.get("/api/architect/v1/closeout").get_json()["data"]
        self.assertEqual(closeout["status"], "present")
        self.assertEqual(closeout["content"]["source_path"], CLOSEOUT_PATH)
        self.assertIn("Accepted evidence.", closeout["content"]["content"])

        review = self.client.get("/api/architect/v1/review").get_json()["data"]
        self.assertEqual(review["repository"]["key"], "first")
        self.assertEqual(review["milestone"]["id"], MILESTONE_ID)
        self.assertEqual(review["git"]["upstream"]["remote_refresh_performed"], False)
        self.assertEqual(review["test_evidence"]["reported_vs_machine_observed"], "reported")
        self.assertEqual(review["context_verification_coverage"]["status"], "MISSING")
        self.assertIn(
            "session starts",
            review["context_verification_coverage"]["coverage_limitation"],
        )
        self.assertNotIn("FIRST_ONLY prompt content.", json.dumps(review))
        self.assertNotIn("FIRST_ONLY context content.", json.dumps(review))
        self.assertEqual(review["governing_documents"][0]["source_type"], "governing_document")

    def test_git_and_document_queries_are_bounded_and_do_not_contact_remote(self) -> None:
        for index in range(45):
            (self.first / f"untracked-{index:02d}.txt").write_text("x\n", encoding="utf-8")
        with patch("repoctl.workflow.git_state.subprocess.run", wraps=subprocess.run) as run_git:
            git_response = self.client.get("/api/architect/v1/git")
        self.assertEqual(git_response.status_code, 200)
        git = git_response.get_json()["data"]
        self.assertEqual(git["branch"], _git(self.first, "branch", "--show-current"))
        self.assertEqual(git["head"], _git(self.first, "rev-parse", "HEAD"))
        self.assertEqual(git["working_tree"]["classification"], "unstaged_only")
        self.assertEqual(git["working_tree"]["untracked"]["total_count"], 52)
        self.assertEqual(git["working_tree"]["untracked"]["returned_count"], 40)
        self.assertEqual(git["working_tree"]["untracked"]["omitted_count"], 12)
        self.assertFalse(git["upstream"]["remote_refresh_performed"])
        self.assertTrue(all("fetch" not in call.args[0] for call in run_git.call_args_list))

        inventory = self.client.get("/api/architect/v1/documents").get_json()["data"]["documents"]
        self.assertEqual({item["role"] for item in inventory}, set(DOCUMENTS))
        self.assertTrue(all(item["status"] == "present" for item in inventory))
        document = self.client.get("/api/architect/v1/documents/context").get_json()["data"]
        self.assertEqual(document["role"], "context")
        self.assertEqual(document["content"]["source_path"], DOCUMENTS["context"])
        self.assertIn("FIRST_ONLY context content.", document["content"]["content"])
        self.assertEqual(self.client.get("/api/architect/v1/documents/parking_lot").status_code, 200)
        unsupported = self.client.get("/api/architect/v1/documents/not_a_role")
        self.assertEqual(unsupported.status_code, 404)
        self.assertEqual(unsupported.get_json()["error"]["code"], "unsupported_document_role")
        with patch(
            "repoctl.web.architect_contract.inspect_git_state",
            side_effect=WorkflowGitStateError("test failure"),
        ):
            failed_git = self.client.get("/api/architect/v1/git")
        self.assertEqual(failed_git.status_code, 503)
        self.assertEqual(failed_git.get_json()["error"]["code"], "git_inspection_failed")

    def test_governing_document_missing_unsafe_and_oversized_states_are_explicit(self) -> None:
        context_path = self.first / DOCUMENTS["context"]
        context_path.unlink()
        inventory = self.client.get("/api/architect/v1/documents").get_json()["data"]["documents"]
        context = next(item for item in inventory if item["role"] == "context")
        self.assertEqual(context["status"], "missing")
        missing = self.client.get("/api/architect/v1/documents/context")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.get_json()["error"]["code"], "missing_source")

        context_path.symlink_to(self.root / "outside.md")
        unsafe_inventory = self.client.get("/api/architect/v1/documents").get_json()["data"]["documents"]
        context = next(item for item in unsafe_inventory if item["role"] == "context")
        self.assertEqual(context["status"], "unsafe")
        unsafe = self.client.get("/api/architect/v1/documents/context")
        self.assertEqual(unsafe.status_code, 422)
        self.assertEqual(unsafe.get_json()["error"]["code"], "unsafe_source")

        context_path.unlink()
        context_path.write_text("x" * 128_001, encoding="utf-8")
        oversized_inventory = self.client.get("/api/architect/v1/documents").get_json()["data"]["documents"]
        context = next(item for item in oversized_inventory if item["role"] == "context")
        self.assertEqual(context["status"], "oversized")
        oversized = self.client.get("/api/architect/v1/documents/context")
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(oversized.get_json()["error"]["code"], "source_oversized")

    def test_repository_document_reads_current_working_tree_with_provenance(self) -> None:
        guide_dir = self.first / "docs/guides"
        guide_dir.mkdir()
        tracked = guide_dir / "tracked.md"
        tracked.write_text("committed guide\n", encoding="utf-8")
        _git(self.first, "add", "docs/guides/tracked.md")
        _git(self.first, "commit", "-qm", "add tracked guide")
        tracked.write_text("modified current guide\n", encoding="utf-8")
        untracked = guide_dir / "untracked.md"
        untracked.write_text("new working-tree guide\n", encoding="utf-8")
        original_head = _git(self.first, "rev-parse", "HEAD")
        original_status = _git(self.first, "status", "--porcelain")

        for relative_path, expected in (
            ("docs/guides/tracked.md", "modified current guide"),
            ("docs/guides/untracked.md", "new working-tree guide"),
        ):
            response = self.client.get(
                "/api/architect/v1/repository-document",
                query_string={
                    "path": relative_path,
                    "expected_repository_key": "first",
                    "expected_generation": "0",
                },
            )
            self.assertEqual(response.status_code, 200, response.get_json())
            payload = response.get_json()
            document = payload["data"]
            self.assertEqual(payload["repository"]["key"], "first")
            self.assertEqual(payload["repository"]["repository_id"], make_repository_id(self.first))
            self.assertEqual(document["source_type"], "repository_document")
            self.assertEqual(document["path"], relative_path)
            self.assertEqual(document["working_tree_source"], "current_working_tree")
            self.assertEqual(document["git_state"], "not_collected")
            self.assertFalse(document["truncated"])
            self.assertEqual(document["file_size_bytes"], len(document["content"].encode("utf-8")))
            self.assertEqual(document["bytes_returned"], len(document["content"].encode("utf-8")))
            self.assertIn(expected, document["content"])
            self.assertNotIn("SECOND_ONLY", document["content"])
        self.assertEqual(_git(self.first, "rev-parse", "HEAD"), original_head)
        self.assertEqual(_git(self.first, "status", "--porcelain"), original_status)

        skill = self.first / ".agents/skills/example/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# Example Skill\n", encoding="utf-8")
        readme = self.first / "README.md"
        readme.write_text("# Explicitly allowed root document\n", encoding="utf-8")
        for relative_path in ("README.md", ".agents/skills/example/SKILL.md"):
            response = self.client.get(
                "/api/architect/v1/repository-document",
                query_string={
                    "path": relative_path,
                    "expected_repository_key": "first",
                    "expected_generation": "0",
                },
            )
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(response.get_json()["data"]["path"], relative_path)
        for relative_path in (".github/notes.md", ".repo-control/workflow.md"):
            target = self.first / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("allowlisted text\n", encoding="utf-8")
            response = self.client.get(
                "/api/architect/v1/repository-document",
                query_string={
                    "path": relative_path,
                    "expected_repository_key": "first",
                    "expected_generation": "0",
                },
            )
            self.assertEqual(response.status_code, 200, response.get_json())

        selection = (
            self.state_root
            / make_repository_id(self.first)
            / "milestones"
            / "selection.json"
        )
        selection.unlink()
        response = self.client.get(
            "/api/architect/v1/repository-document",
            query_string={
                "path": "README.md",
                "expected_repository_key": "first",
                "expected_generation": "0",
            },
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(
            response.get_json()["selection"]["status"],
            "not_required",
        )

    def test_repository_document_rejections_have_bounded_explicit_errors(self) -> None:
        guide_dir = self.first / "docs/guides"
        guide_dir.mkdir()
        (guide_dir / "directory.md").mkdir()
        (guide_dir / "invalid.md").write_bytes(b"\xff\xfe")
        (guide_dir / "binary.md").write_bytes(b"# valid UTF-8 prefix\x00binary\n")
        (guide_dir / "escape.md").symlink_to(self.root / "outside.md")
        (guide_dir / "internal-source.md").symlink_to(self.first / "tracked.txt")
        os.mkfifo(guide_dir / "fifo.md")
        (guide_dir / "private-key.md").write_text(
            "-----BEGIN PRIVATE KEY-----\nsecret\n", encoding="utf-8"
        )
        (guide_dir / "truncated.md").write_bytes(
            b"a" * 127_999 + "é".encode("utf-8") + b"tail"
        )
        oversized = guide_dir / "oversized.md"
        with oversized.open("wb") as stream:
            stream.truncate(5_000_001)

        cases = [
            ("", "invalid_query_parameter", 400),
            ("docs/../README.md", "invalid_document_path", 400),
            ("/etc/passwd", "invalid_document_path", 400),
            ("C:/Windows/system.ini", "invalid_document_path", 400),
            (".git/config", "document_path_not_allowed", 403),
            ("src/repoctl/web/app.py", "document_path_not_allowed", 403),
            ("pyproject.toml", "document_path_not_allowed", 403),
            ("docs/secrets/notes.md", "document_path_not_allowed", 403),
            ("docs/.env.production.md", "document_path_not_allowed", 403),
            ("docs/history/old_closeout.md", "historical_document_not_supported", 403),
            ("docs/milestones/M003.001_old_prompt.md", "historical_document_not_supported", 403),
            ("docs/guides/unsupported.pdf", "unsupported_document_type", 415),
            ("docs/guides/missing.md", "missing_repository_document", 404),
            ("docs/guides/directory.md", "unsafe_document_path", 422),
            ("docs/guides/fifo.md", "unsafe_document_path", 422),
            ("docs/guides/escape.md", "unsafe_document_path", 422),
            ("docs/guides/internal-source.md", "unsafe_document_path", 422),
            ("docs/guides/invalid.md", "invalid_document_encoding", 422),
            ("docs/guides/binary.md", "invalid_document_encoding", 422),
            ("docs/guides/private-key.md", "unsafe_document_content", 422),
            ("docs/guides/oversized.md", "repository_document_oversized", 413),
        ]
        for relative_path, expected_code, expected_status in cases:
            with self.subTest(path=relative_path):
                response = self.client.get(
                    "/api/architect/v1/repository-document",
                    query_string={
                        "path": relative_path,
                        "expected_repository_key": "first",
                        "expected_generation": "0",
                    },
                )
                self.assertEqual(response.status_code, expected_status, response.get_json())
                self.assertEqual(response.get_json()["error"]["code"], expected_code)
                self.assertNotIn(str(self.first), response.get_data(as_text=True))

        truncated = self.client.get(
            "/api/architect/v1/repository-document",
            query_string={
                "path": "docs/guides/truncated.md",
                "expected_repository_key": "first",
                "expected_generation": "0",
            },
        )
        self.assertEqual(truncated.status_code, 200)
        payload = truncated.get_json()["data"]
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["file_size_bytes"], 128_005)
        self.assertEqual(payload["bytes_returned"], 127_999)
        self.assertEqual(payload["character_count"], 127_999)
        self.assertTrue(payload["content"].endswith("a"))

    def test_repository_document_detects_change_during_read(self) -> None:
        document = self.first / "docs/guides/changed.md"
        document.parent.mkdir()
        document.write_text("before\n", encoding="utf-8")
        original_read = os.read
        changed = False

        def read_then_change(fd: int, count: int) -> bytes:
            nonlocal changed
            content = original_read(fd, count)
            if not changed:
                changed = True
                document.write_text("changed\n", encoding="utf-8")
            return content

        with patch("repoctl.web.architect_contract.os.read", side_effect=read_then_change):
            response = self.client.get(
                "/api/architect/v1/repository-document",
                query_string={
                    "path": "docs/guides/changed.md",
                    "expected_repository_key": "first",
                    "expected_generation": "0",
                },
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"]["code"], "repository_document_changed")

    def test_repository_document_requires_current_repository_generation(self) -> None:
        path = "/api/architect/v1/repository-document"
        mismatch = self.client.get(
            path,
            query_string={"path": "docs/context/project_context.md", "expected_repository_key": "second", "expected_generation": "0"},
        )
        self.assertEqual(mismatch.status_code, 409)
        self.assertEqual(mismatch.get_json()["error"]["code"], "stale_repository_selection")

        stale_generation = self.client.get(
            path,
            query_string={"path": "docs/context/project_context.md", "expected_repository_key": "first", "expected_generation": "1"},
        )
        self.assertEqual(stale_generation.status_code, 409)
        self.assertEqual(stale_generation.get_json()["error"]["code"], "stale_repository_generation")

        missing_precondition = self.client.get(
            path,
            query_string={"path": "docs/context/project_context.md"},
        )
        self.assertEqual(missing_precondition.status_code, 400)
        self.assertEqual(missing_precondition.get_json()["error"]["code"], "invalid_precondition")

        missing_path = self.client.get(
            path,
            query_string={"expected_repository_key": "first", "expected_generation": "0"},
        )
        self.assertEqual(missing_path.status_code, 400)
        self.assertEqual(missing_path.get_json()["error"]["code"], "invalid_document_path")

        unexpected_milestone = self.client.get(
            path,
            query_string={
                "path": "docs/context/project_context.md",
                "expected_repository_key": "first",
                "expected_generation": "0",
                "expected_milestone_id": "M007.004",
            },
        )
        self.assertEqual(unexpected_milestone.status_code, 400)
        self.assertEqual(
            unexpected_milestone.get_json()["error"]["code"],
            "unsupported_query_parameter",
        )

    def test_unconfigured_roles_and_bounded_workflow_history(self) -> None:
        no_documents_registry = self.root / "no-documents.toml"
        no_documents_registry.write_text(
            "\n".join(
                [
                    'default = "repo"',
                    "",
                    "[repositories.repo]",
                    'name = "Repository Without Documents"',
                    f"path = {json.dumps(str(self.first))}",
                    'milestone_root = "docs/milestones"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        no_documents_app = create_web_app(
            repository_registry_path=no_documents_registry,
            state_root=self.state_root,
        )
        no_documents_client = no_documents_app.test_client()
        inventory = no_documents_client.get("/api/architect/v1/documents").get_json()["data"]["documents"]
        self.assertTrue(all(item["status"] == "not_configured" for item in inventory))
        not_configured = no_documents_client.get("/api/architect/v1/documents/context")
        self.assertEqual(not_configured.status_code, 404)
        self.assertEqual(not_configured.get_json()["error"]["code"], "document_role_not_configured")

        for index in range(35):
            _record(
                self.first,
                self.state_root,
                "decision_recorded",
                f"Non-binding decision {index}.",
                actor_role="architect",
            )
        workflow = self.client.get("/api/architect/v1/workflow").get_json()["data"]
        self.assertEqual(workflow["event_count"], 35)
        self.assertEqual(workflow["events"]["returned_count"], 30)
        self.assertEqual(workflow["events"]["omitted_count"], 5)

    def test_stale_preconditions_and_arbitrary_path_parameters_are_rejected_as_json(self) -> None:
        stale_generation = self.client.get("/api/architect/v1/status?expected_generation=1")
        self.assertEqual(stale_generation.status_code, 409)
        self.assertEqual(stale_generation.mimetype, "application/json")
        self.assertEqual(stale_generation.get_json()["error"]["code"], "stale_repository_generation")

        stale_milestone = self.client.get(
            f"/api/architect/v1/prompt?expected_milestone_id=M003.003"
        )
        self.assertEqual(stale_milestone.status_code, 409)
        self.assertEqual(stale_milestone.get_json()["error"]["code"], "stale_milestone_selection")

        arbitrary_path = self.client.get("/api/architect/v1/prompt?path=../../etc/passwd")
        self.assertEqual(arbitrary_path.status_code, 400)
        self.assertEqual(arbitrary_path.get_json()["error"]["code"], "unsupported_query_parameter")

        duplicate = self.client.get(
            "/api/architect/v1/status?expected_generation=0&expected_generation=0"
        )
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(duplicate.get_json()["error"]["code"], "invalid_query_parameter")

        with patch(
            "repoctl.web.app.build_architect_response",
            return_value={"contract_version": "1", "data": {"oversized": "x" * 512_000}},
        ):
            oversized_response = self.client.get("/api/architect/v1/status")
        self.assertEqual(oversized_response.status_code, 413)
        self.assertEqual(oversized_response.get_json()["error"]["code"], "response_oversized")

    def test_selected_milestone_pages_reassemble_large_utf8_evidence_and_closeout(self) -> None:
        evidence_path = f"docs/milestones/{MILESTONE_ID}_approved_manifest.tsv"
        prompt = self.first / PROMPT_PATH
        prompt.write_text(
            f"# Prompt\nApproved current-milestone evidence: {evidence_path}\n",
            encoding="utf-8",
        )
        prefix = b"x" * 127_999
        multibyte = "😀\tvalue\n".encode("utf-8")
        raw = prefix + multibyte + b"z" * (415_959 - len(prefix) - len(multibyte))
        (self.first / evidence_path).write_bytes(raw)
        common = {
            "expected_repository_key": "first",
            "expected_generation": "0",
            "expected_milestone_id": MILESTONE_ID,
            "path": evidence_path,
        }

        pages = []
        query = dict(common)
        while True:
            response = self.client.get("/api/architect/v1/milestone-document", query_string=query)
            self.assertEqual(response.status_code, 200, response.get_json())
            page = response.get_json()["data"]
            pages.append(page["content"].encode("utf-8"))
            self.assertLessEqual(page["bytes_returned"], 128_000)
            if not page["complete"]:
                query = {
                    **common,
                    "start_byte": str(page["next_offset"]),
                    "source_fingerprint": page["source_fingerprint"],
                }
            else:
                break
        self.assertEqual(b"".join(pages), raw)
        self.assertEqual(pages[0], prefix)
        self.assertEqual(pages[0].decode("utf-8").encode("utf-8"), pages[0])
        self.assertTrue(pages[1].startswith(multibyte))
        self.assertEqual(page["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(page["file_size_bytes"], 415_959)
        self.assertIsNone(page["next_offset"])
        split_character = self.client.get(
            "/api/architect/v1/milestone-document",
            query_string={
                **common,
                "start_byte": "128000",
                "source_fingerprint": page["source_fingerprint"],
            },
        )
        self.assertEqual(split_character.status_code, 400)
        self.assertEqual(split_character.get_json()["error"]["code"], "invalid_document_offset")

        (self.first / CLOSEOUT_PATH).write_bytes(b"c" * 128_000)
        exact_limit = self.client.get("/api/architect/v1/closeout")
        self.assertEqual(exact_limit.status_code, 200)
        self.assertEqual(len(exact_limit.get_json()["data"]["content"]["content"].encode("utf-8")), 128_000)
        (self.first / CLOSEOUT_PATH).write_bytes(b"c" * 128_001)
        oversized = self.client.get("/api/architect/v1/closeout")
        self.assertEqual(oversized.status_code, 200)
        closeout = oversized.get_json()["data"]
        self.assertIsNone(closeout["content"])
        self.assertEqual(closeout["content_paging"]["page_limit_bytes"], 128_000)
        closeout_pages = []
        query = {**common, "path": CLOSEOUT_PATH}
        while True:
            response = self.client.get("/api/architect/v1/milestone-document", query_string=query)
            self.assertEqual(response.status_code, 200, response.get_json())
            page = response.get_json()["data"]
            closeout_pages.append(page["content"].encode("utf-8"))
            if page["complete"]:
                break
            query = {
                **common,
                "path": CLOSEOUT_PATH,
                "start_byte": str(page["next_offset"]),
                "source_fingerprint": page["source_fingerprint"],
            }
        self.assertEqual(b"".join(closeout_pages), b"c" * 128_001)

    def test_selected_milestone_paging_rejects_stale_unapproved_unsafe_and_invalid_sources(self) -> None:
        evidence_path = f"docs/milestones/{MILESTONE_ID}_approved_manifest.tsv"
        prompt = self.first / PROMPT_PATH
        prompt.write_text(f"# Prompt\n{evidence_path}\n", encoding="utf-8")
        source = self.first / evidence_path
        source.write_bytes(b"a" * 128_001)
        args = {
            "expected_repository_key": "first",
            "expected_generation": "0",
            "expected_milestone_id": MILESTONE_ID,
            "path": evidence_path,
        }
        first_page = self.client.get("/api/architect/v1/milestone-document", query_string=args)
        self.assertEqual(first_page.status_code, 200)
        page = first_page.get_json()["data"]

        source.write_bytes(b"b" * 128_001)
        changed = self.client.get(
            "/api/architect/v1/milestone-document",
            query_string={
                **args,
                "start_byte": str(page["next_offset"]),
                "source_fingerprint": page["source_fingerprint"],
            },
        )
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(changed.get_json()["error"]["code"], "milestone_document_changed")

        source.write_bytes(b"a" * 128_001)
        same_content_page = self.client.get(
            "/api/architect/v1/milestone-document",
            query_string=args,
        )
        self.assertEqual(same_content_page.status_code, 200)
        same_content_fingerprint = same_content_page.get_json()["data"]["source_fingerprint"]
        replacement = source.with_name(f"{source.name}.replacement")
        replacement.write_bytes(b"a" * 128_001)
        os.replace(replacement, source)
        replaced = self.client.get(
            "/api/architect/v1/milestone-document",
            query_string={
                **args,
                "start_byte": str(same_content_page.get_json()["data"]["next_offset"]),
                "source_fingerprint": same_content_fingerprint,
            },
        )
        self.assertEqual(replaced.status_code, 409)
        self.assertEqual(replaced.get_json()["error"]["code"], "milestone_document_changed")

        unapproved_path = f"docs/milestones/{MILESTONE_ID}_unapproved.tsv"
        (self.first / unapproved_path).write_bytes(b"not approved\n")
        unapproved = self.client.get(
            "/api/architect/v1/milestone-document",
            query_string={**args, "path": unapproved_path},
        )
        self.assertEqual(unapproved.status_code, 403)
        self.assertEqual(unapproved.get_json()["error"]["code"], "milestone_evidence_not_approved")

        unsafe_path = f"docs/milestones/{MILESTONE_ID}_unsafe.tsv"
        prompt.write_text(f"# Prompt\n{unsafe_path}\n", encoding="utf-8")
        (self.first / unsafe_path).symlink_to(source)
        unsafe = self.client.get(
            "/api/architect/v1/milestone-document",
            query_string={**args, "path": unsafe_path},
        )
        self.assertEqual(unsafe.status_code, 422)
        self.assertEqual(unsafe.get_json()["error"]["code"], "unsafe_document_path")

        private_path = f"docs/milestones/{MILESTONE_ID}_private.tsv"
        prompt.write_text(f"# Prompt\n{private_path}\n", encoding="utf-8")
        (self.first / private_path).write_text(
            "-----BEGIN PRIVATE KEY-----\nnot-for-export\n",
            encoding="utf-8",
        )
        private = self.client.get(
            "/api/architect/v1/milestone-document",
            query_string={**args, "path": private_path},
        )
        self.assertEqual(private.status_code, 422)
        self.assertEqual(private.get_json()["error"]["code"], "unsafe_document_content")
        self.assertNotIn("not-for-export", private.get_data(as_text=True))

        traversal = self.client.get(
            "/api/architect/v1/milestone-document",
            query_string={**args, "path": "../outside.tsv"},
        )
        self.assertEqual(traversal.status_code, 400)
        unsupported = self.client.get(
            "/api/architect/v1/milestone-document",
            query_string={**args, "path": f"docs/milestones/{MILESTONE_ID}_unsupported.json"},
        )
        self.assertEqual(unsupported.status_code, 415)
        self.assertEqual(unsupported.get_json()["error"]["code"], "unsupported_document_type")

    def test_oversized_missing_malformed_and_review_missing_closeout_are_visible(self) -> None:
        prompt = self.first / PROMPT_PATH
        original_prompt = prompt.read_text(encoding="utf-8")
        prompt.write_text("x" * 128_001, encoding="utf-8")
        oversized = self.client.get("/api/architect/v1/prompt")
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(oversized.get_json()["error"]["code"], "source_oversized")
        self.assertNotIn("content", oversized.get_json())
        prompt.write_text(original_prompt, encoding="utf-8")
        prompt.write_text("-----BEGIN PRIVATE KEY-----\nnot-for-export\n", encoding="utf-8")
        private_material = self.client.get("/api/architect/v1/prompt")
        self.assertEqual(private_material.status_code, 422)
        self.assertEqual(private_material.get_json()["error"]["code"], "unsafe_source")
        self.assertNotIn("not-for-export", private_material.get_data(as_text=True))
        prompt.write_text(original_prompt, encoding="utf-8")

        _record(
            self.first,
            self.state_root,
            "prompt_approved",
            "Prompt approved.",
            actor_role="product_owner",
            po_approved=True,
        )
        _record(self.first, self.state_root, "implementation_started", "Started.", actor_role="coder")
        _record(
            self.first,
            self.state_root,
            "closeout_submitted",
            "Closeout submitted.",
            actor_role="coder",
            evidence_source=CLOSEOUT_PATH,
        )
        (self.first / CLOSEOUT_PATH).unlink()
        missing_closeout = self.client.get("/api/architect/v1/closeout")
        self.assertEqual(missing_closeout.status_code, 200)
        missing_data = missing_closeout.get_json()["data"]
        self.assertEqual(missing_data["status"], "required_missing")
        self.assertEqual(missing_data["contract_error"]["code"], "required_closeout_missing")
        self.assertIsNone(missing_data["content"])

        sidecar = self.first / PROMPT_PATH.replace("_prompt.md", "_workflow.md")
        sidecar.write_text("malformed ledger", encoding="utf-8")
        malformed = self.client.get("/api/architect/v1/workflow")
        self.assertEqual(malformed.status_code, 422)
        self.assertEqual(malformed.get_json()["error"]["code"], "malformed_workflow_sidecar")

    def test_repository_isolation_and_api_reads_preserve_all_state(self) -> None:
        before_head = _git(self.first, "rev-parse", "HEAD")
        before_refs = _git(self.first, "for-each-ref", "--format=%(refname) %(objectname)")
        before_index = _git(self.first, "diff", "--cached", "--name-only")
        pointer = self.state_root / make_repository_id(self.first) / "milestones" / "selection.json"
        before_pointer = pointer.read_bytes()
        before_tree = _tree_digest(self.first)

        for endpoint in (
            "/api/architect/v1/status",
            "/api/architect/v1/milestone",
            "/api/architect/v1/prompt",
            "/api/architect/v1/workflow",
            "/api/architect/v1/closeout",
            "/api/architect/v1/git",
            "/api/architect/v1/evidence",
            "/api/architect/v1/documents",
            "/api/architect/v1/documents/context",
            "/api/architect/v1/review",
        ):
            response = self.client.get(endpoint)
            self.assertEqual(response.status_code, 200, endpoint)
            self.assertNotIn("SECOND_ONLY", response.get_data(as_text=True), endpoint)

        self.assertEqual(_git(self.first, "rev-parse", "HEAD"), before_head)
        self.assertEqual(_git(self.first, "for-each-ref", "--format=%(refname) %(objectname)"), before_refs)
        self.assertEqual(_git(self.first, "diff", "--cached", "--name-only"), before_index)
        self.assertEqual(pointer.read_bytes(), before_pointer)
        self.assertEqual(_tree_digest(self.first), before_tree)
        self.assertFalse((self.first / PROMPT_PATH.replace("_prompt.md", "_workflow.md")).exists())
        method_not_allowed = self.client.post("/api/architect/v1/status")
        self.assertEqual(method_not_allowed.status_code, 405)
        self.assertEqual(method_not_allowed.mimetype, "application/json")

    def test_selection_absence_and_repository_switch_have_predictable_errors(self) -> None:
        no_selection_state = self.root / "other-state"
        no_selection_app = create_web_app(repository_registry_path=self.registry, state_root=no_selection_state)
        no_selection_client = no_selection_app.test_client()
        status = no_selection_client.get("/api/architect/v1/status")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.get_json()["selection"]["status"], "none")
        self.assertEqual(status.get_json()["data"]["milestone_selected"], False)
        no_selection = no_selection_client.get("/api/architect/v1/prompt")
        self.assertEqual(no_selection.status_code, 409)
        self.assertEqual(no_selection.get_json()["error"]["code"], "no_selected_milestone")

        self.client.get("/milestones")
        with self.client.session_transaction() as session:
            csrf = session["csrf_token"]
        switched = self.client.post(
            "/repositories/select",
            data={"csrf_token": csrf, "repository_key": "second"},
        )
        self.assertEqual(switched.status_code, 302)
        stale_repo = self.client.get("/api/architect/v1/status?expected_repository_key=first")
        self.assertEqual(stale_repo.status_code, 409)
        self.assertEqual(stale_repo.get_json()["error"]["code"], "stale_repository_selection")
        selected_repo = self.client.get("/api/architect/v1/status?expected_generation=1")
        self.assertEqual(selected_repo.status_code, 200)
        self.assertEqual(selected_repo.get_json()["repository"]["key"], "second")
        self.assertEqual(
            selected_repo.get_json()["repository"]["registry_generation"],
            1,
        )

    def test_missing_selected_prompt_has_a_stable_json_error(self) -> None:
        (self.first / PROMPT_PATH).unlink()
        response = self.client.get("/api/architect/v1/prompt")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.mimetype, "application/json")
        self.assertEqual(response.get_json()["error"]["code"], "missing_prompt")
