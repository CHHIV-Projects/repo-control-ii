from __future__ import annotations

import unittest
from dataclasses import replace

from repoctl.workflow.approved_prompt_preflight import (
    PromptPreflightError,
    PromptPreflightObservation,
    PromptRevisionBinding,
    validate_fixture_prompt_preflight,
)


class ApprovedPromptPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.binding = PromptRevisionBinding(
            repository_key="fixture-repo",
            repository_id="fixture-repo--0123456789ab",
            repository_root="/tmp/fixture-repo",
            milestone_id="M999.001",
            prompt_path="docs/milestones/M999.001_fixture_prompt.md",
            document_id="prompt-document",
            revision_id="approved-prompt-revision",
            content_sha256="a" * 64,
            selection_id="selection-revision-7",
            selection_revision=7,
        )
        self.observation = PromptPreflightObservation(
            binding=self.binding,
            approval_status="APPROVED",
            lifecycle_phase="IN_PROGRESS",
            lifecycle_authorized=True,
            source_mode="database_revision_materialized",
        )

    def _validate(self, observation=None, dirty_entries=(), *, fixture_only=True):
        return validate_fixture_prompt_preflight(
            self.binding,
            observation or self.observation,
            dirty_entries,
            fixture_only=fixture_only,
        )

    def test_exact_approved_materialized_prompt_is_the_only_allowed_dirty_path(self) -> None:
        result = self._validate(
            dirty_entries=[
                {
                    "path": self.binding.prompt_path,
                    "status": "untracked",
                    "content_sha256": self.binding.content_sha256,
                }
            ]
        )
        self.assertTrue(result["accepted"])
        self.assertTrue(result["allowed_prompt_dirty"])
        self.assertEqual(result["unrelated_dirty_paths"], [])

    def test_immutable_database_revision_does_not_require_or_fall_back_to_worktree_prompt(self) -> None:
        observation = replace(self.observation, source_mode="database_revision")
        result = self._validate(observation)
        self.assertTrue(result["accepted"])
        self.assertFalse(result["allowed_prompt_dirty"])
        with self.assertRaisesRegex(PromptPreflightError, "must not fall back"):
            self._validate(
                observation,
                [
                    {
                        "path": self.binding.prompt_path,
                        "status": "modified",
                        "content_sha256": self.binding.content_sha256,
                    }
                ],
            )

    def test_mismatched_selection_and_stale_or_missing_authorization_fail_closed(self) -> None:
        mismatched = (
            replace(self.binding, repository_id="other-repository"),
            replace(self.binding, milestone_id="M999.002"),
            replace(self.binding, revision_id="other-revision"),
            replace(self.binding, content_sha256="b" * 64),
            replace(self.binding, selection_revision=8),
        )
        for binding in mismatched:
            with self.subTest(binding=binding):
                with self.assertRaisesRegex(PromptPreflightError, "does not match"):
                    self._validate(replace(self.observation, binding=binding))

        for observation in (
            replace(self.observation, approval_status="PENDING"),
            replace(self.observation, approval_status="STALE"),
            replace(self.observation, lifecycle_phase="REVIEW"),
            replace(self.observation, lifecycle_authorized=False),
        ):
            with self.subTest(observation=observation):
                with self.assertRaises(PromptPreflightError):
                    self._validate(observation)

    def test_unrelated_or_conflicted_dirty_paths_are_rejected(self) -> None:
        cases = (
            {"path": "src/unrelated.py", "status": "modified", "content_sha256": "c" * 64},
            {"path": "docs/other.md", "status": "untracked", "content_sha256": "d" * 64},
            {"path": "tests/unrelated.py", "status": "staged", "content_sha256": "e" * 64},
            {"path": "config/settings.md", "status": "conflicted", "content_sha256": "f" * 64},
            {
                "path": self.binding.prompt_path,
                "status": "conflicted",
                "content_sha256": self.binding.content_sha256,
            },
            {
                "path": self.binding.prompt_path,
                "status": "modified",
                "content_sha256": "0" * 64,
            },
        )
        for entry in cases:
            with self.subTest(entry=entry):
                with self.assertRaises(PromptPreflightError):
                    self._validate(dirty_entries=[entry])

    def test_nonfixture_callers_cannot_enable_the_exception(self) -> None:
        with self.assertRaisesRegex(PromptPreflightError, "disposable fixtures"):
            self._validate(fixture_only=False)


if __name__ == "__main__":
    unittest.main()
