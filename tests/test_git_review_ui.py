from __future__ import annotations

import hashlib
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from repoctl.web.app import create_web_app
from repoctl.workflow.git_actions import (
    MAX_ACTION_PATHS,
    MAX_STAGE_PATHS,
    MAX_STAGE_PATH_BYTES,
    PreparedGitActions,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _review(file_count: int = 0) -> dict:
    files = [
        {
            "token": f"path-token-{index}",
            "display_path": f"file-{index}.txt",
            "display_label": "Modified",
            "xy": " M",
            "display_original_path": None,
        }
        for index in range(file_count)
    ]
    return {
        "repository_root": str(REPOSITORY_ROOT),
        "branch": {"state": "attached", "name": "main"},
        "head": "a" * 40,
        "workflow_state_label": "Changes present",
        "upstream": {
            "configured": False,
            "ref": None,
            "divergence_state": "unavailable",
            "ahead": 0,
            "behind": 0,
        },
        "upstream_freshness": "No configured upstream.",
        "git_operations": [],
        "files": files,
        "groups": [{"label": "Working tree changes", "files": files}],
        "selected": None,
    }


def _commit_options(staged_count: int, remaining_count: int = 0) -> dict:
    return {
        "available": True,
        "reason": "",
        "staged_paths": [
            {"xy": "A ", "path": f"staged-{index}.txt"}
            for index in range(staged_count)
        ],
        "remaining_paths": [
            {"xy": " M", "path": f"remaining-{index}.txt"}
            for index in range(remaining_count)
        ],
        "branch": "main",
        "head": "a" * 40,
        "diff_bytes": 1,
        "diff_digest": "b" * 64,
        "diff": "diff --git a/staged.txt b/staged.txt",
    }


class GitReviewUITests(unittest.TestCase):
    def _client(self, review: dict, commit_options: dict):
        state_root = tempfile.TemporaryDirectory()
        self.addCleanup(state_root.cleanup)
        app = create_web_app(
            repository_path=str(REPOSITORY_ROOT),
            state_root=Path(state_root.name),
        )
        patches = (
            mock.patch("repoctl.web.app.inspect_git_review", return_value=review),
            mock.patch.object(PreparedGitActions, "options_for_tokens", return_value={}),
            mock.patch.object(PreparedGitActions, "can_commit", return_value=commit_options),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return app.test_client()

    def test_available_staged_sets_link_to_existing_review_and_preserve_commit_form(self) -> None:
        for staged_count, remaining_count in ((12, 4), (16, 0)):
            with self.subTest(staged_count=staged_count):
                client = self._client(
                    _review(file_count=16),
                    _commit_options(staged_count, remaining_count),
                )
                response = client.get("/git-review")
                html = response.get_data(as_text=True)
                self.assertEqual(response.status_code, 200)

                file_section = html.split('<section class="card git-review-files">', 1)[1].split(
                    "</section>", 1
                )[0]
                self.assertIn(f"{staged_count} staged files.", file_section)
                self.assertIn('href="#commit-review">Next: review the staged commit</a>', file_section)
                self.assertEqual(html.count('id="commit-review"'), 1)
                self.assertEqual(html.count('name="action" value="commit"'), 1)
                commit_form = re.search(
                    r'<form method="post" action="/git-review/action/prepare" class="commit-message-form">(.*?)</form>',
                    html,
                    re.DOTALL,
                )
                self.assertIsNotNone(commit_form)
                self.assertIn('name="csrf_token" value="', commit_form.group(1))
                self.assertIn('name="action" value="commit"', commit_form.group(1))
                if remaining_count:
                    self.assertIn("Unstaged / untracked paths that will remain outside the commit", html)

    def test_unavailable_commit_reason_is_visible_without_a_dead_next_action(self) -> None:
        reason = "There are no explicitly staged changes to commit."
        client = self._client(
            _review(file_count=2),
            {"available": False, "reason": reason},
        )
        response = client.get("/git-review")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        file_section = html.split('<section class="card git-review-files">', 1)[1].split(
            "</section>", 1
        )[0]
        self.assertIn(reason, file_section)
        self.assertNotIn('href="#commit-review">Next: review the staged commit</a>', file_section)
        self.assertNotIn('name="action" value="commit"', html)

    def test_server_limits_asset_version_and_stage_redirect_are_explicit(self) -> None:
        client = self._client(_review(file_count=1), _commit_options(1))
        page = client.get("/git-review")
        html = page.get_data(as_text=True)
        asset_digest = hashlib.sha256(
            (REPOSITORY_ROOT / "src/repoctl/web/static/git_review.js").read_bytes()
        ).hexdigest()

        self.assertEqual(page.status_code, 200)
        self.assertRegex(
            html,
            rf'<form id="batch-action-form"[^>]*data-max-stage-paths="{MAX_STAGE_PATHS}"',
        )
        self.assertIn(f'data-max-stage-path-bytes="{MAX_STAGE_PATH_BYTES}"', html)
        self.assertIn(f'data-max-action-paths="{MAX_ACTION_PATHS}"', html)
        self.assertIn(f'/static/git_review.js?v={asset_digest}', html)

        token_match = re.search(r'name="csrf_token" value="([^"]+)"', html)
        self.assertIsNotNone(token_match)
        with mock.patch.object(
            PreparedGitActions,
            "confirm",
            return_value={"message": "Staged 1 path.", "action": "stage"},
        ) as confirm:
            response = client.post(
                "/git-review/action/confirm",
                data={"csrf_token": token_match.group(1), "action": "stage", "token": "prepared"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/git-review?view=changes#changed-files")
        confirm.assert_called_once()


if __name__ == "__main__":
    unittest.main()
