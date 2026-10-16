from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from repoctl.scanner.util import make_repository_id
from repoctl.web.app import create_web_app
from repoctl.workflow.lifecycle import load_milestone_workflow
from repoctl.workflow.milestones import load_milestone_selection


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
    def test_combined_browser_cycle_staleness_errors_and_authority(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = _repo(root, "first", "R003.01")
            second = _repo(root, "second", "R004.01")
            state_root = root / "state"
            client = create_web_app(
                repository_registry_path=_registry(root, first, second),
                state_root=state_root,
            ).test_client()
            client.get("/milestones")
            csrf = _csrf(client)
            client.post("/milestones/select", data={
                "csrf_token": csrf, "repository_key": "first",
                "repository_generation": "0", "milestone_id": "R003.01",
            })
            candidate = load_milestone_selection(first, state_root)["selection"]

            def workflow():
                selected = load_milestone_selection(first, state_root)["selection"]
                return load_milestone_workflow(first, state_root, "docs/milestones", selected)

            def data(action, **fields):
                current = workflow()
                return {
                    "csrf_token": csrf, "repository_key": "first",
                    "repository_generation": "0", "milestone_id": "R003.01",
                    "prompt_path": candidate["prompt_path"],
                    "expected_phase": current["phase"],
                    "expected_event_count": str(current["event_count"]),
                    "event_type": action, "summary": "Controlled browser action",
                    **fields,
                }

            start = data("approve_and_start", actor_role="coder", po_approved="no")
            no_csrf = client.post("/milestones/workflow/event", data={**start, "csrf_token": ""})
            self.assertEqual(no_csrf.status_code, 400)
            self.assertEqual(workflow()["event_count"], 0)
            self.assertEqual(client.post("/milestones/workflow/event", data=start).status_code, 302)
            self.assertEqual(workflow()["phase"], "IN_PROGRESS")
            self.assertEqual(
                [event["actor_role"] for event in workflow()["event_history"]],
                ["product_owner", "product_owner"],
            )
            self.assertEqual(client.post("/milestones/workflow/event", data=start).status_code, 409)
            self.assertEqual(client.post("/milestones/workflow/event", data=data(
                "question_submitted", waiting_category="blocker", waiting_reason="Need answer",
            )).status_code, 302)
            waiting = client.get("/milestones").get_data(as_text=True)
            self.assertIn("Resolve &amp; Resume", waiting)
            self.assertIn("R003.01 Q1", waiting)
            self.assertIn("Resolve R003.01 Q1 and resume R003.01", waiting)
            question_id = workflow()["open_questions"][0]["event_id"]
            self.assertRegex(waiting, rf'<option value="{question_id}"[^>]*selected')
            sidecar = first / workflow()["sidecar_path"]
            before = sidecar.read_bytes()
            invalid = client.post("/milestones/workflow/event", data=data(
                "resolve_and_resume", related_event_id="wrong",
                summary="Preserve this entered answer", lock_in_summary="Preserve binding text",
            ))
            self.assertEqual(invalid.status_code, 400)
            invalid_text = invalid.get_data(as_text=True)
            self.assertIn("Preserve this entered answer", invalid_text)
            self.assertIn("Preserve binding text", invalid_text)
            self.assertEqual(sidecar.read_bytes(), before)
            stale = data("resolve_and_resume", related_event_id=question_id)
            self.assertEqual(client.post("/milestones/workflow/event", data=data(
                "test_result_recorded", evidence_source="controlled", result="PASS",
            )).status_code, 302)
            before = sidecar.read_bytes()
            self.assertEqual(client.post("/milestones/workflow/event", data=stale).status_code, 409)
            self.assertEqual(sidecar.read_bytes(), before)
            self.assertEqual(client.post("/milestones/workflow/event", data=data(
                "resolve_and_resume", related_event_id=question_id, lock_in_summary="Binding test choice",
            )).status_code, 302)
            self.assertEqual(workflow()["phase"], "IN_PROGRESS")
            self.assertEqual(
                [event["event_type"] for event in workflow()["event_history"][-3:]],
                ["answer_recorded", "lock_in_approved", "implementation_resumed"],
            )
            (first / "docs/milestones/R003.01_test_closeout.md").write_text("# Closeout\n", encoding="utf-8")
            self.assertEqual(client.post("/milestones/workflow/event", data=data("closeout_submitted")).status_code, 302)
            review = client.get("/milestones").get_data(as_text=True)
            self.assertIn("Accept &amp; Close", review)
            self.assertNotIn('name="actor_role"', review)
            self.assertNotIn('name="po_approved"', review)
            self.assertEqual(client.post("/milestones/workflow/event", data=data("accept_and_close")).status_code, 302)
            self.assertEqual(workflow()["phase"], "CLOSED")
            self.assertEqual(
                [event["event_type"] for event in workflow()["event_history"][-2:]],
                ["acceptance_recorded", "milestone_closed"],
            )
            self.assertNotIn("data-workflow-form", client.get("/milestones").get_data(as_text=True))
            git_log = subprocess.run(
                ["git", "-C", str(first), "log", "--format=%s"], check=True, capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(git_log, "initial")
            index_changes = subprocess.run(
                ["git", "-C", str(first), "diff", "--cached", "--name-only"], check=True, capture_output=True, text=True,
            ).stdout
            self.assertEqual(index_changes, "")

    def test_deliberate_browser_action_authorizes_approve_and_start(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = _repo(root, "first", "R003.01")
            second = _repo(root, "second", "R004.01")
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
                    "milestone_id": "R003.01",
                },
            )
            self.assertEqual(selected.status_code, 302)
            page = client.get("/milestones")
            text = page.get_data(as_text=True)
            self.assertIn("Lifecycle: PLANNING", text)
            self.assertIn("Product Owner: review the prompt", text)
            self.assertIn("not authenticated", text)
            self.assertIn("MILESTONE WORKFLOW — EXPLICIT ACTIONS", text)
            self.assertIn('data-summary="Approve &amp; Start R003.01"', text)
            self.assertIn('name="expected_phase" value="PLANNING"', text)
            self.assertIn("Action available in PLANNING", text)
            self.assertNotIn('value="question_submitted"', text)

            data = {
                "csrf_token": csrf,
                "repository_key": "first",
                "repository_generation": "0",
                "milestone_id": "R003.01",
                "prompt_path": "docs/milestones/R003.01_test_prompt.md",
                "event_type": "approve_and_start",
                "summary": "Prompt reviewed and approved.",
                "actor_role": "product_owner",
                "expected_phase": "PLANNING",
                "expected_event_count": "0",
            }
            self.assertNotIn('name="actor_role"', text)
            self.assertNotIn('name="po_approved"', text)
            data.pop("actor_role")
            accepted = client.post("/milestones/workflow/event", data=data)
            self.assertEqual(accepted.status_code, 302)
            active = client.get("/milestones").get_data(as_text=True)
            self.assertIn("Lifecycle: IN_PROGRESS", active)
            self.assertIn("Events recorded:</strong> 2", active)
            stale_phase = client.post(
                "/milestones/workflow/event",
                data={
                    **data,
                    "event_type": "implementation_started",
                    "expected_phase": "PLANNING",
                    "summary": "Stale phase submission",
                },
            )
            self.assertEqual(stale_phase.status_code, 409)

    def test_workflow_actions_aliases_and_related_choices_are_milestone_local(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = _repo(root, "first", "R003.01")
            second = _repo(root, "second", "R004.01")
            registry = _registry(root, first, second)
            client = create_web_app(
                repository_registry_path=registry,
                state_root=root / "state",
            ).test_client()
            client.get("/milestones")
            csrf = _csrf(client)
            client.post(
                "/milestones/select",
                data={
                    "csrf_token": csrf,
                    "repository_key": "first",
                    "repository_generation": "0",
                    "milestone_id": "R003.01",
                },
            )
            fragment_args = {
                "repository_key": "first",
                "repository_generation": "0",
                "milestone_id": "R003.01",
                "prompt_path": "docs/milestones/R003.01_test_prompt.md",
            }
            initial_panel = client.get(
                "/milestones/workflow/fragment",
                query_string=fragment_args,
            )
            self.assertIn("Events recorded:</strong> 0", initial_panel.get_data(as_text=True))
            initial_version = re.search(
                r'data-state-version="([^"]+)"',
                initial_panel.get_data(as_text=True),
            )
            self.assertIsNotNone(initial_version)

            def submit(phase: str, event_type: str, summary: str, **fields: str):
                return client.post(
                    "/milestones/workflow/event",
                    data={
                        "csrf_token": csrf,
                        "repository_key": "first",
                        "repository_generation": "0",
                        "milestone_id": "R003.01",
                        "prompt_path": "docs/milestones/R003.01_test_prompt.md",
                        "expected_phase": phase,
                        "event_type": event_type,
                        "summary": summary,
                        "actor_role": "product_owner",
                        **fields,
                    },
                )

            self.assertEqual(
                submit("PLANNING", "prompt_approved", "Approve prompt", po_approved="yes").status_code,
                302,
            )
            self.assertEqual(submit("READY", "implementation_started", "Start work").status_code, 302)
            self.assertEqual(
                submit("IN_PROGRESS", "question_submitted", "Need scope clarification").status_code,
                302,
            )
            self.assertEqual(
                submit("IN_PROGRESS", "question_submitted", "Need second clarification").status_code,
                302,
            )
            self.assertEqual(
                submit("IN_PROGRESS", "bug_discovered", "Incorrect status display").status_code,
                302,
            )
            self.assertEqual(
                submit("IN_PROGRESS", "bug_discovered", "Incorrect event count").status_code,
                302,
            )

            panel = client.get(
                "/milestones/workflow/fragment",
                query_string=fragment_args,
            )
            text = panel.get_data(as_text=True)
            self.assertEqual(panel.status_code, 200)
            final_version = re.search(r'data-state-version="([^"]+)"', text)
            self.assertIsNotNone(final_version)
            self.assertNotEqual(initial_version.group(1), final_version.group(1))
            self.assertIn("Events recorded:</strong> 6", text)
            self.assertIn("R003.01 Q1 · question_submitted", text)
            self.assertIn("R003.01 Q2 · question_submitted", text)
            self.assertIn("R003.01 B1 · bug_discovered", text)
            self.assertIn("R003.01 B2 · bug_discovered", text)
            self.assertIn('data-alias="R003.01 Q1"', text)
            self.assertIn('data-actions="answer_recorded"', text)
            self.assertIn('data-actions="bug_classified"', text)
            self.assertIn('data-summary="Response to {related}"', text)
            self.assertIn('value="answer_recorded"', text)
            answer_options = re.findall(r'<option value="[^"]+"[^>]*data-actions="answer_recorded"[^>]*>', text)
            self.assertEqual(len(answer_options), 2)
            self.assertTrue(all("selected" not in option for option in answer_options))
            self.assertEqual(panel.headers["Cache-Control"], "no-store")

    def test_phase_actions_follow_the_six_lifecycle_states(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = _repo(root, "first", "R003.01")
            second = _repo(root, "second", "R004.01")
            closeout = first / "docs" / "milestones" / "R003.01_test_closeout.md"
            closeout.write_text("# Closeout\n", encoding="utf-8")
            registry = _registry(root, first, second)
            client = create_web_app(
                repository_registry_path=registry,
                state_root=root / "state",
            ).test_client()
            client.get("/milestones")
            csrf = _csrf(client)
            client.post(
                "/milestones/select",
                data={
                    "csrf_token": csrf,
                    "repository_key": "first",
                    "repository_generation": "0",
                    "milestone_id": "R003.01",
                },
            )

            def page_text() -> str:
                return client.get("/milestones").get_data(as_text=True)

            def assert_actions(text: str, present: set[str], absent: set[str] = frozenset()) -> None:
                for event_type in present:
                    self.assertRegex(text, rf'<option value="{event_type}"')
                for event_type in absent:
                    self.assertNotRegex(text, rf'<option value="{event_type}"')

            def submit(phase: str, event_type: str, summary: str, **fields: str):
                return client.post(
                    "/milestones/workflow/event",
                    data={
                        "csrf_token": csrf,
                        "repository_key": "first",
                        "repository_generation": "0",
                        "milestone_id": "R003.01",
                        "prompt_path": "docs/milestones/R003.01_test_prompt.md",
                        "expected_phase": phase,
                        "event_type": event_type,
                        "summary": summary,
                        "actor_role": "product_owner",
                        **fields,
                    },
                )

            assert_actions(
                page_text(),
                {"approve_and_start", "decision_recorded", "lock_in_approved"},
                {"implementation_started", "question_submitted"},
            )
            self.assertEqual(
                submit("PLANNING", "prompt_approved", "Approve", po_approved="yes").status_code,
                302,
            )
            assert_actions(page_text(), {"implementation_started"}, {"prompt_approved"})

            self.assertEqual(
                submit("READY", "implementation_started", "Start implementation").status_code,
                302,
            )
            in_progress = page_text()
            assert_actions(
                in_progress,
                {"question_submitted", "bug_discovered", "closeout_submitted"},
                {"answer_recorded", "bug_classified", "bug_fixed"},
            )
            self.assertEqual(
                submit(
                    "IN_PROGRESS",
                    "question_submitted",
                    "Clarification needed",
                    waiting_category="clarification",
                    waiting_reason="Confirm expected behavior.",
                ).status_code,
                302,
            )
            waiting = page_text()
            assert_actions(
                waiting,
                {"resolve_and_resume", "answer_recorded", "decision_recorded"},
                {"question_submitted", "implementation_resumed"},
            )

            answer_option = re.search(
                r'<option value="([^"]+)"\s+data-actions="answer_recorded(?:,resolve_and_resume)?"',
                waiting,
            )
            self.assertIsNotNone(answer_option)
            question_id = answer_option.group(1)
            rejected_answer = submit(
                "WAITING",
                "answer_recorded",
                "",
                related_event_id="",
            )
            self.assertEqual(rejected_answer.status_code, 400)
            rejected_text = rejected_answer.get_data(as_text=True)
            self.assertRegex(
                rejected_text,
                rf'<option value="{re.escape(question_id)}"[^>]*selected',
            )
            answered = submit(
                "WAITING",
                "answer_recorded",
                "Clarification resolved",
                related_event_id=question_id,
            )
            self.assertEqual(answered.status_code, 302)
            assert_actions(page_text(), {"implementation_resumed"})
            self.assertEqual(
                submit("WAITING", "implementation_resumed", "Resume work").status_code,
                302,
            )
            assert_actions(page_text(), {"question_submitted", "closeout_submitted"})

            self.assertEqual(
                submit("IN_PROGRESS", "closeout_submitted", "Submit closeout").status_code,
                302,
            )
            review = page_text()
            assert_actions(
                review,
                {"accept_and_close", "correction_requested", "review_requested"},
                {"milestone_closed"},
            )
            self.assertEqual(
                submit(
                    "REVIEW",
                    "acceptance_recorded",
                    "Accept reviewed closeout",
                    po_approved="yes",
                ).status_code,
                302,
            )
            assert_actions(page_text(), {"accept_and_close"})
            self.assertEqual(
                submit(
                    "REVIEW",
                    "milestone_closed",
                    "Close accepted milestone",
                    po_approved="yes",
                ).status_code,
                302,
            )
            closed = page_text()
            self.assertIn("Lifecycle: CLOSED", closed)
            self.assertNotIn('data-workflow-form', closed)

    def test_stale_milestone_and_repository_workflow_posts_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = _repo(root, "first", "R003.01")
            second = _repo(root, "second", "R004.01")
            second_prompt = first / "docs" / "milestones" / "R003.02_second_prompt.md"
            second_prompt.write_text("# Second prompt\n", encoding="utf-8")
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
                    "milestone_id": "R003.01",
                },
            )
            stale_page = client.get("/milestones").get_data(as_text=True)
            self.assertIn('name="prompt_path" value="docs/milestones/R003.01_test_prompt.md"', stale_page)

            client.post(
                "/milestones/select",
                data={
                    "csrf_token": csrf,
                    "repository_key": "first",
                    "repository_generation": "0",
                    "milestone_id": "R003.02",
                },
            )
            stale_milestone = client.post(
                "/milestones/workflow/event",
                data={
                    "csrf_token": csrf,
                    "repository_key": "first",
                    "repository_generation": "0",
                    "milestone_id": "R003.01",
                    "prompt_path": "docs/milestones/R003.01_test_prompt.md",
                    "event_type": "prompt_approved",
                    "summary": "Stale form",
                    "actor_role": "product_owner",
                    "po_approved": "yes",
                },
            )
            self.assertEqual(stale_milestone.status_code, 409)
            self.assertFalse(
                (first / "docs/milestones/R003.01_test_workflow.md").exists()
            )

            client.post(
                "/repositories/select",
                data={"csrf_token": csrf, "repository_key": "second"},
            )
            stale_repository = client.post(
                "/milestones/workflow/event",
                data={
                    "csrf_token": csrf,
                    "repository_key": "first",
                    "repository_generation": "0",
                    "milestone_id": "R003.02",
                    "prompt_path": "docs/milestones/R003.02_second_prompt.md",
                    "event_type": "prompt_approved",
                    "summary": "Stale repository form",
                    "actor_role": "product_owner",
                    "po_approved": "yes",
                },
            )
            self.assertEqual(stale_repository.status_code, 409)
            self.assertFalse((first / "docs/milestones/R003.02_second_workflow.md").exists())
            self.assertFalse((second / "docs/milestones/R004.01_test_workflow.md").exists())

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
            self.assertIn("preserving distinct workflow events", first_text)
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
