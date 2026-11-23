from __future__ import annotations

import hashlib
import json
from pathlib import Path

from repoctl.procedure_governance import (
    ProcedureBundle,
    _render_artifacts,
    _render_lock,
    load_bundle,
)
from repoctl.web.repository_registry import RepositoryEntry


_TEST_SOURCE_BYTES = {
    "bootstrap.md": (
        b"# Test Coder Bootstrap\n\n"
        b"Release: {{release}}\n"
        b"Overlay: {{project_overlay_path}}\n"
        b"Workflow: {{workflow_core_path}}\n"
    ),
    "workflow_core.md": b"# Test Workflow Core\n",
    "skills/repo-control-implementation/SKILL.md": b"# Test implementation skill\n",
    "skills/repo-control-blocker-bug-escalation/SKILL.md": b"# Test blocker skill\n",
    "skills/repo-control-validation-closeout/SKILL.md": b"# Test validation skill\n",
}

_FIVE_ARTIFACTS = (
    ("bootstrap.md", ".github/copilot-instructions.md", "bootstrap"),
    ("workflow_core.md", ".repo-control/workflow_core.md", "copy"),
    (
        "skills/repo-control-implementation/SKILL.md",
        ".agents/skills/repo-control-implementation/SKILL.md",
        "copy",
    ),
    (
        "skills/repo-control-blocker-bug-escalation/SKILL.md",
        ".agents/skills/repo-control-blocker-bug-escalation/SKILL.md",
        "copy",
    ),
    (
        "skills/repo-control-validation-closeout/SKILL.md",
        ".agents/skills/repo-control-validation-closeout/SKILL.md",
        "copy",
    ),
)


def _create_procedure_fixture(destination: Path, release: str) -> ProcedureBundle:
    if release not in {"1.3.0", "1.4.0"}:
        raise ValueError(f"Unsupported test procedure release: {release}")
    destination.mkdir(parents=True)
    for relative, content in _TEST_SOURCE_BYTES.items():
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    artifacts = list(_FIVE_ARTIFACTS)
    if release == "1.4.0":
        artifacts.append(("bootstrap.md", "AGENTS.md", "bootstrap"))

    manifest_lines = [
        "schema_version = 1",
        f"release = {json.dumps(release)}",
        "overlay_schema_version = 1",
        'overlay_marker = "<!-- repo-control-project-overlay:v1 -->"',
        'lock_path = ".repo-control/shared-procedure.lock.toml"',
        'host_entrypoint = ".github/copilot-instructions.md"',
        "",
    ]
    for relative, content in _TEST_SOURCE_BYTES.items():
        manifest_lines.extend(
            [
                "[[sources]]",
                f"path = {json.dumps(relative)}",
                f'sha256 = "{hashlib.sha256(content).hexdigest()}"',
                "",
            ]
        )
    for source, target, renderer in artifacts:
        manifest_lines.extend(
            [
                "[[artifacts]]",
                f"source = {json.dumps(source)}",
                f"target = {json.dumps(target)}",
                f"renderer = {json.dumps(renderer)}",
                "",
            ]
        )
    (destination / "manifest.toml").write_text(
        "\n".join(manifest_lines),
        encoding="utf-8",
    )
    return load_bundle(destination)


def create_five_artifact_v130_bundle(destination: Path) -> ProcedureBundle:
    """Create the test-owned published 1.3.0 five-artifact fixture."""
    return _create_procedure_fixture(destination, "1.3.0")


def create_six_artifact_v140_bundle(destination: Path) -> ProcedureBundle:
    """Create the test-owned published 1.4.0 six-artifact fixture."""
    return _create_procedure_fixture(destination, "1.4.0")


def materialize_test_bundle(bundle: ProcedureBundle, entry: RepositoryEntry) -> None:
    artifacts = _render_artifacts(bundle, entry)
    for relative, content in artifacts:
        target = entry.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    lock = entry.path / bundle.lock_path
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(_render_lock(bundle, entry, artifacts))
