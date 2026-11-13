from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from repoctl import information_store
from repoctl.information_store import (
    ImportSourceError,
    InformationStore,
    StoreIdentity,
    V1FixtureBundle,
    build_historical_closeout_path_reconciliation_receipt,
    build_historical_path_reconciliation_receipt,
    _parse_sidecar_bytes,
)
from repoctl.workflow import lifecycle
from tests.test_sqlite_workflow_poc import _closeout_history


class HistoricalPathReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = StoreIdentity(
            repository_id="vocab-app--0d81efec80ef",
            repository_key="vocab-app",
            repository_root="/home/chuck/projects/vocab-app",
            milestone_id="M001.000",
            prompt_path=(
                "docs/milestones/"
                "M001.000_vocab_app_repo_control_onboarding_reconnaissance_prompt.md"
            ),
            selection_id="fixture-selection",
            selection_revision=0,
        )
        self.prompt_bytes = b"# historical prompt fixture\n"
        self.workflow_path = (
            "docs/milestones/"
            "M001.000_vocab_app_repo_control_onboarding_reconnaissance_workflow.md"
        )
        self.workflow_bytes = (
            "<!-- repoctl-workflow-ledger:v1 -->\n"
            "```json\n"
            + json.dumps(
                {
                    "schema_version": lifecycle.WORKFLOW_LEDGER_SCHEMA_VERSION,
                    "repository_id": self.identity.repository_id,
                    "milestone_id": self.identity.milestone_id,
                    "prompt_path": (
                        "milestones/"
                        "M001.000_vocab_app_repo_control_onboarding_reconnaissance_prompt.md"
                    ),
                    "events": [],
                }
            )
            + "\n```\n"
        ).encode("utf-8")
        self.prompt_sha256 = information_store._HISTORICAL_PATH_RECONCILIATIONS[
            "M001.000"
        ]["prompt_sha256"]
        self.workflow_sha256 = information_store._HISTORICAL_PATH_RECONCILIATIONS[
            "M001.000"
        ]["workflow_sha256"]

    def _receipt(self) -> dict[str, str]:
        with patch(
            "repoctl.information_store._sha256",
            side_effect=[self.prompt_sha256, self.workflow_sha256, "a" * 64],
        ):
            receipt = build_historical_path_reconciliation_receipt(
                self.identity,
                original_declared_prompt_path=(
                    "milestones/"
                    "M001.000_vocab_app_repo_control_onboarding_reconnaissance_prompt.md"
                ),
                workflow_path=self.workflow_path,
                prompt_path=self.identity.prompt_path,
                prompt_bytes=self.prompt_bytes,
                workflow_bytes=self.workflow_bytes,
            )
        if receipt is None:
            self.fail("The exact registered historical source pair should produce a receipt.")
        return receipt

    def test_exact_identity_and_source_hashes_are_required(self) -> None:
        receipt = self._receipt()
        self.assertEqual(receipt["category"], "historical_reconciled")
        self.assertEqual(receipt["original_declared_prompt_path"].split("/", 1)[0], "milestones")
        self.assertEqual(receipt["mapped_target_prompt_path"], self.identity.prompt_path)
        self.assertEqual(len(receipt["reconciliation_receipt_sha256"]), 64)

        other_identity = replace(self.identity, repository_id="another-repository")
        self.assertIsNone(
            build_historical_path_reconciliation_receipt(
                other_identity,
                original_declared_prompt_path=receipt["original_declared_prompt_path"],
                workflow_path=self.workflow_path,
                prompt_path=self.identity.prompt_path,
                prompt_bytes=self.prompt_bytes,
                workflow_bytes=self.workflow_bytes,
            )
        )

        with patch(
            "repoctl.information_store._sha256",
            side_effect=[self.prompt_sha256, "0" * 64],
        ):
            changed_source = build_historical_path_reconciliation_receipt(
                self.identity,
                original_declared_prompt_path=receipt["original_declared_prompt_path"],
                workflow_path=self.workflow_path,
                prompt_path=self.identity.prompt_path,
                prompt_bytes=self.prompt_bytes,
                workflow_bytes=self.workflow_bytes,
            )
        self.assertIsNone(changed_source)

    def test_sidecar_alias_requires_the_matching_receipt(self) -> None:
        receipt = self._receipt()
        with patch(
            "repoctl.information_store._sha256",
            side_effect=[self.prompt_sha256, self.workflow_sha256, "a" * 64],
        ):
            events = _parse_sidecar_bytes(
                self.workflow_bytes,
                self.identity,
                reconciliation_receipt=receipt,
                prompt_path=self.identity.prompt_path,
                prompt_bytes=self.prompt_bytes,
                workflow_path=self.workflow_path,
            )
        self.assertEqual(events, [])

        with self.assertRaises(ImportSourceError):
            _parse_sidecar_bytes(self.workflow_bytes, self.identity)

        invalid_receipt = {
            **receipt,
            "mapped_target_prompt_path": "docs/milestones/not-the-approved-target.md",
        }
        with patch(
            "repoctl.information_store._sha256",
            side_effect=[self.prompt_sha256, self.workflow_sha256, "a" * 64],
        ):
            with self.assertRaises(ImportSourceError):
                _parse_sidecar_bytes(
                    self.workflow_bytes,
                    self.identity,
                    reconciliation_receipt=invalid_receipt,
                    prompt_path=self.identity.prompt_path,
                    prompt_bytes=self.prompt_bytes,
                    workflow_path=self.workflow_path,
                )

    def _closeout_sources(self) -> tuple[dict, bytes, bytes, bytes, StoreIdentity, dict]:
        rule = dict(information_store._HISTORICAL_PATH_RECONCILIATIONS["M001.000"])
        identity = replace(self.identity, prompt_path=rule["canonical_prompt_path"])
        prompt_bytes = b"# Current canonical prompt\n"
        closeout_bytes = b"# Current canonical closeout\n"
        events = _closeout_history()
        declared_closeout_path = rule["original_declared_closeout_path"]
        closeout_submissions = [
            event for event in events if event["event_type"] == "closeout_submitted"
        ]
        for event in closeout_submissions:
            event["evidence_path"] = declared_closeout_path
        ledger = lifecycle._empty_ledger(
            identity.repository_id,
            {"milestone_id": identity.milestone_id, "prompt_path": identity.prompt_path},
        )
        ledger["prompt_path"] = rule["original_declared_prompt_path"]
        ledger["events"] = events
        workflow_bytes = lifecycle._render_sidecar(ledger).encode("utf-8")
        rule["prompt_sha256"] = information_store._sha256(prompt_bytes)
        rule["workflow_sha256"] = information_store._sha256(workflow_bytes)
        rule["closeout_sha256"] = information_store._sha256(closeout_bytes)
        rule["closeout_submission_count"] = len(closeout_submissions)
        prompt_path = rule["canonical_prompt_path"]
        workflow_path = prompt_path.removesuffix("_prompt.md") + "_workflow.md"
        closeout_path = rule["canonical_closeout_path"]
        with patch.dict(
            information_store._HISTORICAL_PATH_RECONCILIATIONS,
            {"M001.000": rule},
        ):
            prompt_receipt = build_historical_path_reconciliation_receipt(
                identity,
                original_declared_prompt_path=rule["original_declared_prompt_path"],
                workflow_path=workflow_path,
                prompt_path=prompt_path,
                prompt_bytes=prompt_bytes,
                workflow_bytes=workflow_bytes,
            )
            if prompt_receipt is None:
                self.fail("The exact registered prompt mapping should produce a receipt.")
            closeout_receipt = build_historical_closeout_path_reconciliation_receipt(
                identity,
                prompt_reconciliation_receipt=prompt_receipt,
                prompt_path=prompt_path,
                prompt_bytes=prompt_bytes,
                workflow_path=workflow_path,
                workflow_bytes=workflow_bytes,
                closeout_path=closeout_path,
                closeout_bytes=closeout_bytes,
                canonical_target_match_count=1,
            )
        if closeout_receipt is None:
            self.fail("The exact approved closeout mapping should produce a receipt.")
        return (
            {"prompt": prompt_receipt, "closeout": closeout_receipt},
            prompt_bytes,
            workflow_bytes,
            closeout_bytes,
            identity,
            rule,
        )

    def test_exact_closeout_alias_is_qualified_and_keeps_original_event_paths(self) -> None:
        receipts, prompt_bytes, workflow_bytes, closeout_bytes, identity, rule = self._closeout_sources()
        closeout_receipt = receipts["closeout"]
        self.assertEqual(
            closeout_receipt["category"],
            "historical_reconciled_unverified_original",
        )
        self.assertEqual(
            closeout_receipt["original_declared_closeout_path"],
            "milestones/M001.000_vocab_app_repo_control_onboarding_reconnaissance_closeout.md",
        )
        self.assertEqual(
            closeout_receipt["mapped_target_closeout_path"],
            "docs/milestones/M001.000_vocab_app_repo_control_onboarding_reconnaissance_closeout.md",
        )
        self.assertEqual(closeout_receipt["original_closeout_sha256"], "UNKNOWN/NOT VERIFIED")
        self.assertEqual(closeout_receipt["original_closeout_bytes"], "UNKNOWN/NOT VERIFIED")
        self.assertEqual(closeout_receipt["event_time_revision_identity"], "UNKNOWN/NOT VERIFIED")

        with tempfile.TemporaryDirectory(prefix="repoctl_closeout_reconciliation_", dir="/tmp") as temporary:
            store = InformationStore.initialize_disposable(
                Path(temporary) / "fixture.sqlite3",
                identity,
                allow_unsupported_runtime_for_fixture=True,
            )
            bundle = V1FixtureBundle(
                prompt_path=identity.prompt_path,
                closeout_path=closeout_receipt["mapped_target_closeout_path"],
                workflow_path=identity.prompt_path.removesuffix("_prompt.md") + "_workflow.md",
                prompt_bytes=prompt_bytes,
                closeout_bytes=closeout_bytes,
                workflow_bytes=workflow_bytes,
                reconciliation_receipt=receipts["prompt"],
                closeout_reconciliation_receipt=closeout_receipt,
            )
            with patch.dict(
                information_store._HISTORICAL_PATH_RECONCILIATIONS,
                {"M001.000": rule},
            ):
                first = store.import_v1_fixture(bundle)
                retry = store.import_v1_fixture(bundle)
            self.assertEqual(first, retry)
            self.assertEqual(
                first["historical_closeout_reconciliation"],
                closeout_receipt,
            )
            events = store.snapshot()["events"]
            submissions = [
                event for event in events if event["event_type"] == "closeout_submitted"
            ]
            self.assertEqual(len(submissions), 1)
            self.assertEqual(
                submissions[0]["evidence_path"],
                closeout_receipt["original_declared_closeout_path"],
            )
            self.assertEqual(
                store.list_workflow_document_links(
                    first["closeout_document_id"],
                    first["closeout_revision_id"],
                ),
                [],
            )
            revision = store.get_revision(
                first["closeout_document_id"],
                first["closeout_revision_id"],
            )
            self.assertEqual(
                revision["provenance"]["historical_closeout_path_reconciliation"],
                closeout_receipt,
            )

    def test_closeout_alias_rejects_changed_identity_path_hash_or_ambiguity(self) -> None:
        receipts, prompt_bytes, workflow_bytes, closeout_bytes, identity, rule = self._closeout_sources()
        closeout_receipt = receipts["closeout"]
        arguments = {
            "prompt_reconciliation_receipt": receipts["prompt"],
            "prompt_path": identity.prompt_path,
            "prompt_bytes": prompt_bytes,
            "workflow_path": identity.prompt_path.removesuffix("_prompt.md") + "_workflow.md",
            "workflow_bytes": workflow_bytes,
            "closeout_path": rule["canonical_closeout_path"],
            "closeout_bytes": closeout_bytes,
            "canonical_target_match_count": 1,
        }
        cases = (
            (replace(identity, repository_id="other"), arguments),
            (replace(identity, prompt_path="docs/milestones/other_prompt.md"), arguments),
            (replace(identity, milestone_id="M001.002"), arguments),
            (identity, {**arguments, "closeout_path": "docs/milestones/other_closeout.md"}),
            (identity, {**arguments, "closeout_bytes": closeout_bytes + b"changed"}),
            (identity, {**arguments, "canonical_target_match_count": 2}),
            (
                identity,
                {
                    **arguments,
                    "workflow_bytes": workflow_bytes.replace(
                        b"milestones/M001.000_vocab_app_repo_control_onboarding_reconnaissance_closeout.md",
                        b"milestones/other_closeout.md",
                    ),
                },
            ),
        )
        with patch.dict(information_store._HISTORICAL_PATH_RECONCILIATIONS, {"M001.000": rule}):
            for candidate_identity, candidate_arguments in cases:
                with self.subTest(candidate_identity=candidate_identity, candidate_arguments=candidate_arguments):
                    self.assertIsNone(
                        build_historical_closeout_path_reconciliation_receipt(
                            candidate_identity,
                            **candidate_arguments,
                        )
                    )

        with tempfile.TemporaryDirectory(prefix="repoctl_bad_closeout_reconciliation_", dir="/tmp") as temporary:
            store = InformationStore.initialize_disposable(
                Path(temporary) / "fixture.sqlite3",
                identity,
                allow_unsupported_runtime_for_fixture=True,
            )
            invalid_receipt = {
                **closeout_receipt,
                "mapped_target_closeout_path": "docs/milestones/forged_closeout.md",
            }
            with patch.dict(
                information_store._HISTORICAL_PATH_RECONCILIATIONS,
                {"M001.000": rule},
            ):
                with self.assertRaises(ImportSourceError):
                    store.import_v1_fixture(
                        V1FixtureBundle(
                            prompt_path=identity.prompt_path,
                            closeout_path=rule["canonical_closeout_path"],
                            workflow_path=arguments["workflow_path"],
                            prompt_bytes=prompt_bytes,
                            closeout_bytes=closeout_bytes,
                            workflow_bytes=workflow_bytes,
                            reconciliation_receipt=receipts["prompt"],
                            closeout_reconciliation_receipt=invalid_receipt,
                        )
                    )


if __name__ == "__main__":
    unittest.main()
