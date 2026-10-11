from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from repoctl.scanner.util import make_repository_id
from repoctl.web.app import create_web_app


DOCUMENTS = {
    "coding_agent_rules": "docs/context/coding_agent_rules.md",
    "workflow": "docs/context/project_workflow.md",
    "context": "docs/context/project_context.md",
    "architecture": "docs/context/project_architecture.md",
}


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _repo(root: Path, name: str, milestone_id: str) -> Path:
    repo = root / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("content\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "initial")
    prompt = repo / "docs/milestones" / f"{milestone_id}_web_prompt.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("# Web packet prompt\n\nPrompt source assertion.\n", encoding="utf-8")
    for role, relative_path in DOCUMENTS.items():
        document = repo / relative_path
        document.parent.mkdir(parents=True, exist_ok=True)
        document.write_text(f"# {role}\n\n{role} web source.\n", encoding="utf-8")
    return repo


def _registry(root: Path, first: Path, second: Path) -> Path:
    entries = ['default = "first"', ""]
    for key, repo in (("first", first), ("second", second)):
        entries.extend(
            [
                f"[repositories.{key}]",
                f"name = {json.dumps(key.title())}",
                f"path = {json.dumps(str(repo))}",
                'milestone_root = "docs/milestones"',
                "",
                f"[repositories.{key}.documents]",
            ]
        )
        entries.extend(
            f"{role} = {json.dumps(path)}"
            for role, path in DOCUMENTS.items()
        )
        entries.append("")
    path = root / "repositories.toml"
    path.write_text("\n".join(entries), encoding="utf-8")
    return path


def _csrf(client) -> str:
    with client.session_transaction() as session:
        return session["csrf_token"]


class HandoffWebTests(unittest.TestCase):
    def test_generate_and_copy_coder_packet_without_workflow_or_git_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = _repo(root, "first", "M003.001")
            second = _repo(root, "second", "M004.001")
            (second / "docs/milestones/M004.001_web_prompt.md").write_text(
                "# Second-only secret marker\n",
                encoding="utf-8",
            )
            registry = _registry(root, first, second)
            state_root = root / "state"
            client = create_web_app(repository_registry_path=registry, state_root=state_root).test_client()
            client.get("/milestones")
            csrf = _csrf(client)
            selected = client.post(
                "/milestones/select",
                data={
                    "csrf_token": csrf,
                    "repository_key": "first",
                    "repository_generation": "0",
                    "milestone_id": "M003.001",
                },
            )
            self.assertEqual(selected.status_code, 302)
            before_head = _git(first, "rev-parse", "HEAD")
            before_refs = _git(first, "for-each-ref", "--format=%(refname) %(objectname)")
            before_index = _git(first, "diff", "--cached", "--name-only")
            pointer = (
                state_root
                / make_repository_id(first)
                / "milestones"
                / "selection.json"
            )
            before_pointer = pointer.read_bytes()

            response = client.post(
                "/milestones/handoff",
                data={
                    "csrf_token": csrf,
                    "repository_key": "first",
                    "repository_generation": "0",
                    "milestone_id": "M003.001",
                    "prompt_path": "docs/milestones/M003.001_web_prompt.md",
                    "packet_type": "coder",
                },
            )

            self.assertEqual(response.status_code, 200)
            text = response.get_data(as_text=True)
            self.assertIn("Coder Handoff Packet", text)
            self.assertIn("Prompt source assertion.", text)
            self.assertIn("coding_agent_rules web source.", text)
            self.assertNotIn("Second-only secret marker", text)
            self.assertIn("Copy packet", text)
            self.assertIn("handoff.js", text)
            self.assertFalse((first / "docs/milestones/M003.001_web_workflow.md").exists())
            self.assertEqual(_git(first, "rev-parse", "HEAD"), before_head)
            self.assertEqual(
                _git(first, "for-each-ref", "--format=%(refname) %(objectname)"),
                before_refs,
            )
            self.assertEqual(_git(first, "diff", "--cached", "--name-only"), before_index)
            self.assertEqual(pointer.read_bytes(), before_pointer)

    def test_packet_generation_rejects_stale_milestone_and_repository_forms(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = _repo(root, "first", "M003.001")
            second = _repo(root, "second", "M004.001")
            registry = _registry(root, first, second)
            state_root = root / "state"
            client = create_web_app(repository_registry_path=registry, state_root=state_root).test_client()
            client.get("/milestones")
            csrf = _csrf(client)
            client.post(
                "/milestones/select",
                data={
                    "csrf_token": csrf,
                    "repository_key": "first",
                    "repository_generation": "0",
                    "milestone_id": "M003.001",
                },
            )
            (first / "docs/milestones/M003.002_other_prompt.md").write_text("# Other\n", encoding="utf-8")
            client.post(
                "/milestones/select",
                data={
                    "csrf_token": csrf,
                    "repository_key": "first",
                    "repository_generation": "0",
                    "milestone_id": "M003.002",
                },
            )
            stale_milestone = client.post(
                "/milestones/handoff",
                data={
                    "csrf_token": csrf,
                    "repository_key": "first",
                    "repository_generation": "0",
                    "milestone_id": "M003.001",
                    "prompt_path": "docs/milestones/M003.001_web_prompt.md",
                    "packet_type": "coder",
                },
            )
            self.assertEqual(stale_milestone.status_code, 409)

            client.post(
                "/repositories/select",
                data={"csrf_token": csrf, "repository_key": "second"},
            )
            stale_repository = client.post(
                "/milestones/handoff",
                data={
                    "csrf_token": csrf,
                    "repository_key": "first",
                    "repository_generation": "0",
                    "milestone_id": "M003.002",
                    "prompt_path": "docs/milestones/M003.002_other_prompt.md",
                    "packet_type": "coder",
                },
            )
            self.assertEqual(stale_repository.status_code, 409)
            self.assertFalse((first / "docs/milestones/M003.001_web_workflow.md").exists())
            self.assertFalse((second / "docs/milestones/M004.001_web_workflow.md").exists())
