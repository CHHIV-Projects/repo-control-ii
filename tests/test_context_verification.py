from __future__ import annotations

import unittest

from repoctl.workflow.context_verification import (
    build_verification_coverage,
    make_verification_record,
    normalize_git_snapshot,
    snapshot_digest,
    validate_verification_record,
)


class ContextVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = {
            "repository": {"key": "repo", "id": "repo--123", "root": "/work/repo"},
            "milestone": {
                "id": "M003.001",
                "prompt_path": "docs/milestones/M003.001_prompt.md",
                "prompt_sha256": "c" * 64,
                "lifecycle": "IN_PROGRESS",
                "authorization": {"authorized": True, "status": "AUTHORIZED"},
                "closeout": {"exists": False, "path": None},
            },
            "workflow_evidence": {"test_result_count": 0},
            "approved_architect_dispatches": {"inspection_status": "available"},
            "procedure": {
                "inspection_status": "available",
                "status": "CURRENT",
                "overlay_valid": True,
            },
            "git": {"inspection_status": "available"},
        }

    def test_verification_record_validation_and_provenance(self) -> None:
        record = make_verification_record(
            trigger="NEW",
            snapshot=self.snapshot,
            result="VERIFIED",
            git_boundary_acknowledged=True,
        )

        self.assertTrue(validate_verification_record(record))
        self.assertEqual(record["trigger_provenance"], "agent_reported")
        self.assertEqual(record["session_identity"], "unverified")
        self.assertEqual(record["evidence_provenance"], "repo_control_observed_local_state")
        self.assertEqual(record["provider_delivery"], "unverified")

        record["authority_snapshot"]["milestone"]["id"] = "M003.002"
        self.assertFalse(validate_verification_record(record))

    def test_coverage_reports_missing_invalid_stale_and_current_records(self) -> None:
        record = make_verification_record(
            trigger="HANDOFF",
            snapshot=self.snapshot,
            result="PARTIAL_UNVERIFIED",
            git_boundary_acknowledged=False,
        )
        events = [
            {
                "event_id": "valid",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "event_type": "context_verification_recorded",
                "actor_role": "coder",
                "po_approved": False,
                "phase_before": "IN_PROGRESS",
                "phase_after": "IN_PROGRESS",
                "context_verification": record,
            },
            {
                "event_id": "invalid",
                "timestamp": "2026-01-01T00:00:01+00:00",
                "event_type": "context_verification_recorded",
                "actor_role": "coder",
                "po_approved": False,
                "phase_before": "IN_PROGRESS",
                "phase_after": "WAITING",
                "context_verification": {"trigger": "invented"},
            },
        ]
        changed_snapshot = {
            **self.snapshot,
            "milestone": {"id": "M003.002"},
        }

        coverage = build_verification_coverage(events, changed_snapshot)
        self.assertEqual(coverage["status"], "INVALID")
        self.assertEqual(coverage["valid_record_count"], 1)
        self.assertEqual(coverage["invalid_record_count"], 1)
        self.assertEqual(coverage["stale_record_count"], 1)
        self.assertEqual(coverage["observed_trigger_counts"], {"HANDOFF": 1})
        self.assertEqual(coverage["trigger_provenance"], "agent_reported")
        self.assertEqual(coverage["provider_delivery"], "unverified")

        stale = build_verification_coverage(events[:1], changed_snapshot)
        self.assertEqual(stale["status"], "STALE")

        current_coverage = build_verification_coverage(events[:1], self.snapshot)
        self.assertEqual(current_coverage["status"], "CURRENT")
        self.assertEqual(current_coverage["current_record_count"], 1)

        missing = build_verification_coverage([], self.snapshot)
        self.assertEqual(missing["status"], "MISSING")
        self.assertIn("missing records do not prove", missing["coverage_limitation"])

    def test_material_authority_changes_make_prior_verification_stale(self) -> None:
        record = make_verification_record(
            trigger="AUTHORITY_TRANSITION",
            snapshot=self.snapshot,
            result="VERIFIED",
            git_boundary_acknowledged=True,
        )
        event = {
            "event_id": "event",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "event_type": "context_verification_recorded",
            "actor_role": "coder",
            "po_approved": False,
            "phase_before": "IN_PROGRESS",
            "phase_after": "IN_PROGRESS",
            "context_verification": record,
        }
        variations = [
            {"repository": {"key": "other", "id": "repo--123", "root": "/work/repo"}},
            {"milestone": {**self.snapshot["milestone"], "prompt_sha256": "new-prompt"}},
            {
                "milestone": {
                    **self.snapshot["milestone"],
                    "lifecycle": "WAITING",
                    "authorization": {"authorized": False},
                }
            },
            {"approved_architect_dispatches": {"inspection_status": "available", "sha256": "new-guidance"}},
            {"procedure": {"inspection_status": "available", "canonical_release": "1.2.0"}},
            {"git": {"inspection_status": "available", "head": "new-head"}},
        ]
        for update in variations:
            with self.subTest(update=update):
                changed = {**self.snapshot, **update}
                coverage = build_verification_coverage([event], changed)
                self.assertEqual(coverage["status"], "STALE")
                self.assertEqual(coverage["stale_record_count"], 1)

    def test_git_snapshot_is_bounded_redacted_and_excludes_its_own_sidecar(self) -> None:
        git_state = {
            "repository_root": "/work/repo",
            "branch": "main",
            "head": "abc123",
            "workflow_state": "unstaged_only",
            "git_operation_in_progress": False,
            "git_operations": [],
            "working_tree": {
                "is_clean": False,
                "entries": [
                    {"kind": "untracked", "xy": "??", "path": "docs/milestones/workflow.md"},
                    {"kind": "untracked", "xy": "??", "path": ".env/token.pem"},
                ],
            },
        }

        snapshot = normalize_git_snapshot(git_state, {"docs/milestones/workflow.md"})
        self.assertEqual(snapshot["dirty_entry_count"], 1)
        self.assertEqual(snapshot["dirty_entries"][0]["path"], "<redacted-sensitive-path>")
        self.assertEqual(snapshot["excluded_workflow_sidecar"], ["docs/milestones/workflow.md"])
        self.assertEqual(snapshot["working_tree_state"], "unstaged_only")
        self.assertEqual(len(snapshot["dirty_entries_sha256"]), 64)
        self.assertEqual(snapshot["dirty_entries_sha256"], snapshot_digest([{
            "kind": "untracked",
            "path": "<redacted-sensitive-path>",
            "xy": "??",
        }]))


if __name__ == "__main__":
    unittest.main()
