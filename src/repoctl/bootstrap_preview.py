from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TRUSTED_PYTHON = REPOSITORY_ROOT / ".venv" / "bin" / "python"

_REPOSITORY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_REPOSITORY_ID = re.compile(r"[a-z0-9._-]+--[0-9a-f]{12}\Z")


def build_bootstrap_preview(
    *,
    repository_key: str,
    repository_id: str,
    repository_root: str | Path,
    python_executable: str | Path = TRUSTED_PYTHON,
) -> dict[str, object]:
    root = Path(repository_root)
    python = Path(python_executable)
    if not _REPOSITORY_KEY.fullmatch(repository_key):
        raise ValueError("Repository key must be a managed-repository identifier.")
    if not _REPOSITORY_ID.fullmatch(repository_id):
        raise ValueError("Repository ID must be a canonical managed-repository identity.")
    if not root.is_absolute() or not python.is_absolute():
        raise ValueError("Repository root and trusted Python executable must be absolute paths.")

    identity_arguments = [
        "--repository-root",
        str(root),
        "--repository-key",
        repository_key,
        "--repository-id",
        repository_id,
    ]

    def command(module: str) -> str:
        return shlex.join([
            str(python),
            "-m",
            module,
            *identity_arguments,
        ])

    return {
        "schema": "repo-control.bootstrap-preview.v1",
        "preview_only": True,
        "target": {
            "repository_key": repository_key,
            "repository_id": repository_id,
            "repository_root": str(root),
        },
        "trusted_runtime": {
            "python_executable": str(python),
            "adapter_source_root": str(REPOSITORY_ROOT),
            "trust_review_required": True,
        },
        "providers": {
            "copilot": {
                "config_path": ".github/hooks/repo_control_context.json",
                "registration": {
                    "version": 1,
                    "hooks": {
                        "SessionStart": [
                            {
                                "type": "command",
                                "command": command("repoctl.copilot_bootstrap"),
                                "cwd": ".",
                                "timeoutSec": 15,
                            }
                        ]
                    },
                },
            },
            "codex": {
                "config_path": ".codex/hooks.json",
                "registration": {
                    "hooks": {
                        "SessionStart": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": command("repoctl.codex_bootstrap"),
                                        "timeout": 20,
                                        "async": False,
                                    }
                                ]
                            }
                        ]
                    },
                },
            },
        },
        "limitations": [
            "Preview does not install or trust hooks and does not inspect the target worktree.",
            "Canonical coder-context remains the authority for repository identity, selection, prompt, and lifecycle.",
            "Host discovery, normal project trust, and fresh-session delivery require separate target-repository acceptance.",
        ],
    }


def format_bootstrap_preview(preview: dict[str, object]) -> str:
    return json.dumps(preview, ensure_ascii=False, indent=2) + "\n"
