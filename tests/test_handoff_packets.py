from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from repoctl.workflow.handoff_packets import (
    MAX_SOURCE_BYTES,
    HandoffPacketError,
    generate_handoff_packet,
)
from repoctl.workflow.lifecycle import record_milestone_event
from repoctl.workflow.milestones import (
    discover_milestones,
    load_milestone_selection,
    save_milestone_selection,
)
from repoctl.scanner.util import make_repository_id


DOCUMENTS = {
    "coding_agent_rules": "docs/context/coding_agent_rules.md",
    "workflow": "docs/context/project_workflow.md",
    "context": "docs/context/project_context.md",
    "architecture": "docs/context/project_architecture.md",
    "parking_lot": "docs/context/canonical_parking_lot.md",
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


def _repo(root: Path, milestone_id: str = "M003.001") -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("content\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "initial")
    prompt = repo / "docs" / "milestones" / f"{milestone_id}_handoff_test_prompt.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("# Prompt\n\nDo the bounded work.\n", encoding="utf-8")
    for role, relative_path in DOCUMENTS.items():
        document = repo / relative_path
        document.parent.mkdir(parents=True, exist_ok=True)
        document.write_text(f"# {role}\n\n{role} source text.\n", encoding="utf-8")
    return repo


def _candidate(repo: Path, milestone_id: str = "M003.001") -> dict:
    return next(item for item in discover_milestones(repo)["candidates"] if item["milestone_id"] == milestone_id)


def _record(repo: Path, state_root: Path, candidate: dict, event_type: str, summary: str, **kwargs):
    return record_milestone_event(
        repo,
        state_root,
        "docs/milestones",
        candidate,
        event_type=event_type,
        summary=summary,
        actor_role=kwargs.pop("actor_role", "coder"),
        **kwargs,
    )


class HandoffPacketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repo = _repo(self.root)
        self.state_root = self.root / "state"
        self.candidate = _candidate(self.repo)
        save_milestone_selection(self.repo, self.state_root, self.candidate["milestone_id"])
        self.repository_id = make_repository_id(self.repo)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _packet(self, packet_type: str, candidate: dict | None = None, documents: dict | None = None) -> str:
        return generate_handoff_packet(
            repository_root=self.repo,
            repository_name="Test Repo",
            repository_id=self.repository_id,
            state_root=self.state_root,
            milestone_root="docs/milestones",
            documents=documents if documents is not None else DOCUMENTS,
            candidate=candidate if candidate is not None else self.candidate,
            packet_type=packet_type,
        )

    def test_coder_packet_contains_prompt_rules_binding_decisions_and_waiting_state(self) -> None:
        _record(
            self.repo, self.state_root, self.candidate, "prompt_approved",
            "Prompt approved", actor_role="product_owner", po_approved=True,
        )
        _record(self.repo, self.state_root, self.candidate, "implementation_started", "Started")
        _record(
            self.repo, self.state_root, self.candidate, "lock_in_approved",
            "Use the documented bounded API.", actor_role="product_owner", po_approved=True,
        )
        _record(
            self.repo, self.state_root, self.candidate, "decision_recorded",
            "This note must not become a binding instruction.",
        )
        _record(
            self.repo, self.state_root, self.candidate, "question_submitted",
            "Need an answer before continuing.",
            waiting_category="clarification",
            waiting_reason="Awaiting a response.",
        )
        before_head = _git(self.repo, "rev-parse", "HEAD")
        before_refs = _git(self.repo, "for-each-ref", "--format=%(refname) %(objectname)")
        before_index = _git(self.repo, "diff", "--cached", "--name-only")
        sidecar = self.repo / "docs/milestones/M003.001_handoff_test_workflow.md"
        before_sidecar = sidecar.read_bytes()
        pointer = self.state_root / self.repository_id / "milestones" / "selection.json"
        before_pointer = pointer.read_bytes()

        packet = self._packet("coder")
        repeated_packet = self._packet("coder")

        self.assertIn("# Coder Handoff Packet", packet)
        self.assertIn("Test Repo", packet)
        self.assertIn("M003.001", packet)
        self.assertIn("Do the bounded work.", packet)
        self.assertIn("coding_agent_rules source text.", packet)
        self.assertIn("workflow source text.", packet)
        self.assertIn("Use the documented bounded API.", packet)
        self.assertIn("Awaiting a response.", packet)
        self.assertIn("WAITING: implementation is paused", packet)
        self.assertIn("Git Mutation Prohibition", packet)
        self.assertNotIn("This note must not become a binding instruction.", packet)
        self.assertIn("M003.001_handoff_test_workflow.md", packet)
        self.assertEqual(
            re.sub(r"Generated at \(UTC\): `[^`]+`", "Generated at (UTC): `<time>`", packet),
            re.sub(r"Generated at \(UTC\): `[^`]+`", "Generated at (UTC): `<time>`", repeated_packet),
        )
        self.assertEqual(_git(self.repo, "rev-parse", "HEAD"), before_head)
        self.assertEqual(
            _git(self.repo, "for-each-ref", "--format=%(refname) %(objectname)"),
            before_refs,
        )
        self.assertEqual(_git(self.repo, "diff", "--cached", "--name-only"), before_index)
        self.assertEqual(sidecar.read_bytes(), before_sidecar)
        self.assertEqual(pointer.read_bytes(), before_pointer)

    def test_architect_packet_contains_closeout_git_test_and_reported_evidence(self) -> None:
        closeout_path = self.repo / "docs/milestones/M003.001_handoff_test_closeout.md"
        closeout_path.write_text("# Closeout\n\nObserved result: done.\n", encoding="utf-8")
        candidate = _candidate(self.repo)
        _record(
            self.repo, self.state_root, candidate, "prompt_approved",
            "Prompt approved", actor_role="product_owner", po_approved=True,
        )
        _record(self.repo, self.state_root, candidate, "implementation_started", "Started")
        _record(
            self.repo, self.state_root, candidate, "lock_in_approved",
            "Preserve stable interfaces.", actor_role="product_owner", po_approved=True,
        )
        _record(
            self.repo, self.state_root, candidate, "test_result_recorded",
            "Focused check passed.",
            evidence_source="pytest tests/test_example.py",
            result="PASS",
        )
        _record(self.repo, self.state_root, candidate, "closeout_submitted", "Submitted.")

        packet = self._packet("architect", candidate)

        self.assertIn("# Architect Review Packet", packet)
        self.assertIn("Do the bounded work.", packet)
        self.assertIn("Preserve stable interfaces.", packet)
        self.assertIn("Observed result: done.", packet)
        self.assertIn("Branch:", packet)
        self.assertIn("HEAD:", packet)
        self.assertIn("ahead/behind unavailable", packet)
        self.assertIn("pytest tests/test_example.py", packet)
        self.assertIn("not independently verified by Repo Control", packet)
        self.assertIn("Source References", packet)
        self.assertIn("no source artifact, lifecycle, selection, or Git state was changed.", packet)

    def test_legacy_packet_does_not_infer_lifecycle_or_decision_history(self) -> None:
        legacy_repo = self.root / "legacy"
        legacy_repo.mkdir()
        _git(legacy_repo, "init", "-q")
        _git(legacy_repo, "config", "user.email", "test@example.com")
        _git(legacy_repo, "config", "user.name", "Test User")
        (legacy_repo / "tracked.txt").write_text("content\n", encoding="utf-8")
        _git(legacy_repo, "add", "tracked.txt")
        _git(legacy_repo, "commit", "-qm", "initial")
        prompt = legacy_repo / "docs/milestones/R003.01_handoff_test_prompt.md"
        prompt.parent.mkdir(parents=True)
        prompt.write_text("# Historical prompt\n", encoding="utf-8")
        legacy_id = make_repository_id(legacy_repo)
        candidate = _candidate(legacy_repo, "R003.01")
        legacy_state = self.root / "legacy-state"
        save_milestone_selection(legacy_repo, legacy_state, "R003.01")

        packet = generate_handoff_packet(
            repository_root=legacy_repo,
            repository_name="Legacy",
            repository_id=legacy_id,
            state_root=legacy_state,
            milestone_root="docs/milestones",
            documents={},
            candidate=candidate,
            packet_type="architect",
        )

        self.assertIn("LIMITED LEGACY MODE", packet)
        self.assertIn("lifecycle phase, approvals, and decision history are unavailable", packet)
        self.assertNotIn("Lifecycle: `PLANNING`", packet)
        self.assertIn("Workflow/decision history unavailable", packet)

    def test_stale_selection_missing_document_and_oversized_source_fail_visibly(self) -> None:
        second_prompt = self.repo / "docs/milestones/M003.002_other_prompt.md"
        second_prompt.write_text("# Other\n", encoding="utf-8")
        save_milestone_selection(self.repo, self.state_root, "M003.002")
        with self.assertRaisesRegex(HandoffPacketError, "changed or is invalid"):
            self._packet("coder")
        save_milestone_selection(self.repo, self.state_root, "M003.001")

        incomplete_documents = {"workflow": DOCUMENTS["workflow"]}
        with self.assertRaisesRegex(HandoffPacketError, "coding_agent_rules"):
            self._packet("coder", documents=incomplete_documents)

        source = self.repo / DOCUMENTS["coding_agent_rules"]
        source.write_text("x" * (MAX_SOURCE_BYTES + 1), encoding="utf-8")
        with self.assertRaisesRegex(HandoffPacketError, "no content was truncated"):
            self._packet("coder")

    def test_missing_configured_source_and_malformed_workflow_fail_visibly(self) -> None:
        rules = self.repo / DOCUMENTS["coding_agent_rules"]
        rules.unlink()
        with self.assertRaisesRegex(HandoffPacketError, "Cannot read required source"):
            self._packet("coder")
        rules.write_text("# Rules\n", encoding="utf-8")
        sidecar = self.repo / "docs/milestones/M003.001_handoff_test_workflow.md"
        sidecar.write_text("malformed ledger", encoding="utf-8")
        with self.assertRaisesRegex(HandoffPacketError, "invalid"):
            self._packet("coder")

    def test_symlinked_configured_document_is_rejected(self) -> None:
        source = self.repo / DOCUMENTS["coding_agent_rules"]
        source.unlink()
        source.symlink_to(self.repo / "tracked.txt")
        with self.assertRaisesRegex(HandoffPacketError, "symlinked"):
            self._packet("coder")

    def test_private_key_material_in_a_source_blocks_packet_generation(self) -> None:
        source = self.repo / DOCUMENTS["coding_agent_rules"]
        source.write_text(
            "# Rules\n\n-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(HandoffPacketError, "private-key material"):
            self._packet("coder")

    def test_coder_correction_packet_includes_bounded_request_and_closeout(self) -> None:
        _record(
            self.repo, self.state_root, self.candidate, "prompt_approved",
            "Prompt approved", actor_role="product_owner", po_approved=True,
        )
        _record(self.repo, self.state_root, self.candidate, "implementation_started", "Started")
        closeout = self.repo / "docs/milestones/M003.001_handoff_test_closeout.md"
        closeout.write_text("# Existing closeout\n\nPrior evidence.\n", encoding="utf-8")
        candidate = _candidate(self.repo)
        _record(self.repo, self.state_root, candidate, "closeout_submitted", "Ready for review.")
        _record(
            self.repo, self.state_root, candidate, "correction_requested",
            "Clarify the source limit and update its test.",
            actor_role="architect",
        )

        packet = self._packet("coder", candidate)

        self.assertIn("correction mode", packet)
        self.assertIn("Bounded Correction Request", packet)
        self.assertIn("Clarify the source limit and update its test.", packet)
        self.assertIn("Existing Closeout for Correction Context", packet)
        self.assertIn("Prior evidence.", packet)
