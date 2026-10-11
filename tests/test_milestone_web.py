from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from repoctl.scanner.util import make_repository_id
from repoctl.web.app import create_web_app


def _git(repo: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))


def _repo(root: Path, name: str, milestone_id: str) -> Path:
    repo = root / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("content\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "initial")
    prompt = repo / "docs" / "milestones" / f"{milestone_id}_test_prompt.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("# Prompt\n", encoding="utf-8")
    return repo


def _registry(
    root: Path,
    first: Path,
    second: Path,
    milestone_roots: tuple[str, str] = ("docs/milestones", "docs/milestones"),
) -> Path:
    path = root / "repositories.toml"
    path.write_text(
        "\n".join(
            [
                'default = "first"',
                "",
                "[repositories.first]",
                'name = "First Repository"',
                f"path = {json.dumps(str(first))}",
                f"milestone_root = {json.dumps(milestone_roots[0])}",
                "",
                "[repositories.second]",
                'name = "Second Repository"',
                f"path = {json.dumps(str(second))}",
                f"milestone_root = {json.dumps(milestone_roots[1])}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _csrf(client) -> str:
    with client.session_transaction() as session:
        return session["csrf_token"]


class MilestoneWebTests(unittest.TestCase):
    def test_configured_milestone_root_is_server_selected_and_used_for_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = _repo(root, "first", "R003.01")
            second = _repo(root, "second", "R004.01")
            configured_root = first / "project" / "milestones"
            configured_root.mkdir(parents=True)
            (configured_root / "M003.001_custom_prompt.md").write_text("# Prompt\n", encoding="utf-8")
            registry = _registry(root, first, second, ("project/milestones", "docs/milestones"))
            state_root = root / "state"
            client = create_web_app(repository_registry_path=registry, state_root=state_root).test_client()

            page = client.get("/milestones")
            page_text = page.get_data(as_text=True)
            self.assertIn("project/milestones/", page_text)
            self.assertIn("M003.001", page_text)
            self.assertNotIn("R003.01", page_text)

            response = client.post(
                "/milestones/select",
                data={
                    "csrf_token": _csrf(client),
                    "repository_key": "first",
                    "repository_generation": "0",
                    "milestone_id": "M003.001",
                },
            )
            self.assertEqual(response.status_code, 302)
            pointer = state_root / make_repository_id(first) / "milestones" / "selection.json"
            self.assertEqual(
                json.loads(pointer.read_text(encoding="utf-8"))["prompt_path"],
                "project/milestones/M003.001_custom_prompt.md",
            )

    def test_selection_is_explicit_scoped_and_survives_app_reconstruction(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = _repo(root, "first", "R003.01")
            second = _repo(root, "second", "R004.01")
            registry = _registry(root, first, second)
            state_root = root / "state"
            app = create_web_app(repository_registry_path=registry, state_root=state_root)
            client = app.test_client()

            first_page = client.get("/milestones")
            first_text = first_page.get_data(as_text=True)
            self.assertEqual(first_page.status_code, 200)
            self.assertIn("No milestone selected", first_text)
            self.assertIn("R003.01", first_text)
            self.assertIn("First Repository", first_text)
            self.assertIn("does not establish approval", first_text)
            self.assertIn(">Milestone</a>", first_text)

            csrf = _csrf(client)
            selected_first = client.post(
                "/milestones/select",
                data={
                    "csrf_token": csrf,
                    "repository_key": "first",
                    "repository_generation": "0",
                    "milestone_id": "R003.01",
                },
                follow_redirects=True,
            )
            self.assertEqual(selected_first.status_code, 200)
            self.assertIn("Selected milestone: R003.01", selected_first.get_data(as_text=True))
            self.assertIn("Closeout: Not present", selected_first.get_data(as_text=True))

            switched = client.post(
                "/repositories/select",
                data={"csrf_token": csrf, "repository_key": "second"},
                follow_redirects=True,
            )
            self.assertEqual(switched.status_code, 200)
            second_page = client.get("/milestones")
            self.assertIn("Second Repository", second_page.get_data(as_text=True))
            self.assertIn("No milestone selected", second_page.get_data(as_text=True))
            self.assertNotIn("Selected milestone: R003.01", second_page.get_data(as_text=True))

            client.post(
                "/milestones/select",
                data={
                    "csrf_token": csrf,
                    "repository_key": "second",
                    "repository_generation": "1",
                    "milestone_id": "R004.01",
                },
            )
            returned = client.post(
                "/repositories/select",
                data={"csrf_token": csrf, "repository_key": "first"},
                follow_redirects=True,
            )
            self.assertEqual(returned.status_code, 200)
            self.assertIn("Selected milestone: R003.01", client.get("/milestones").get_data(as_text=True))

            restarted_app = create_web_app(repository_registry_path=registry, state_root=state_root)
            restarted = restarted_app.test_client().get("/milestones")
            self.assertIn("First Repository", restarted.get_data(as_text=True))
            self.assertIn("Selected milestone: R003.01", restarted.get_data(as_text=True))

            pointer_first = state_root / make_repository_id(first) / "milestones" / "selection.json"
            pointer_second = state_root / make_repository_id(second) / "milestones" / "selection.json"
            self.assertTrue(pointer_first.is_file())
            self.assertTrue(pointer_second.is_file())
            self.assertEqual(json.loads(pointer_first.read_text(encoding="utf-8"))["milestone_id"], "R003.01")
            self.assertEqual(json.loads(pointer_second.read_text(encoding="utf-8"))["milestone_id"], "R004.01")

    def test_stale_and_invalid_selection_requests_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = _repo(root, "first", "R003.01")
            second = _repo(root, "second", "R004.01")
            registry = _registry(root, first, second)
            state_root = root / "state"
            client = create_web_app(repository_registry_path=registry, state_root=state_root).test_client()
            page = client.get("/milestones")
            text = page.get_data(as_text=True)
            generation_match = re.search(r'name="repository_generation" value="([^"]+)"', text)
            self.assertIsNotNone(generation_match)
            csrf = _csrf(client)

            client.post(
                "/repositories/select",
                data={"csrf_token": csrf, "repository_key": "second"},
            )
            stale = client.post(
                "/milestones/select",
                data={
                    "csrf_token": csrf,
                    "repository_key": "first",
                    "repository_generation": generation_match.group(1),
                    "milestone_id": "R003.01",
                },
            )
            self.assertEqual(stale.status_code, 409)
            self.assertFalse((state_root / make_repository_id(first) / "milestones" / "selection.json").exists())
            self.assertFalse((state_root / make_repository_id(second) / "milestones" / "selection.json").exists())

            invalid = client.post(
                "/milestones/select",
                data={
                    "csrf_token": csrf,
                    "repository_key": "second",
                    "repository_generation": "1",
                    "milestone_id": "R003.01",
                },
            )
            self.assertEqual(invalid.status_code, 400)
            self.assertFalse((state_root / make_repository_id(second) / "milestones" / "selection.json").exists())

    def test_invalid_saved_pointer_is_visible_and_can_be_reselected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _repo(root, "first", "R003.01")
            registry = _registry(root, repo, _repo(root, "second", "R004.01"))
            state_root = root / "state"
            pointer = state_root / make_repository_id(repo) / "milestones" / "selection.json"
            pointer.parent.mkdir(parents=True)
            pointer.write_text("{broken", encoding="utf-8")
            client = create_web_app(repository_registry_path=registry, state_root=state_root).test_client()

            page = client.get("/milestones")
            text = page.get_data(as_text=True)
            self.assertEqual(page.status_code, 200)
            self.assertIn("Saved selection needs attention", text)
            self.assertIn("malformed", text)
            self.assertIn("R003.01", text)

            csrf = _csrf(client)
            repaired = client.post(
                "/milestones/select",
                data={
                    "csrf_token": csrf,
                    "repository_key": "first",
                    "repository_generation": "0",
                    "milestone_id": "R003.01",
                },
                follow_redirects=True,
            )
            self.assertEqual(repaired.status_code, 200)
            self.assertIn("Selected milestone: R003.01", repaired.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
