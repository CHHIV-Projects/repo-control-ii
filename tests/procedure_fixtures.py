from __future__ import annotations

import shutil
from pathlib import Path

from repoctl.procedure_governance import ProcedureBundle, load_bundle


def copy_five_artifact_v130_bundle(source: Path, destination: Path) -> ProcedureBundle:
    """Create a synthetic 1.3.0 five-artifact manifest using current sources."""
    shutil.copytree(source, destination)
    manifest_path = destination / "manifest.toml"
    manifest = manifest_path.read_text(encoding="utf-8")
    prefix, separator, final_artifact = manifest.rpartition("\n[[artifacts]]")
    if not separator or 'target = "AGENTS.md"' not in final_artifact:
        raise AssertionError("Canonical bundle does not end with the Codex artifact mapping.")
    manifest = prefix + "\n"
    if 'release = "1.4.0"' not in manifest:
        raise AssertionError("Expected the canonical 1.4.0 bundle fixture.")
    manifest = manifest.replace('release = "1.4.0"', 'release = "1.3.0"', 1)
    manifest_path.write_text(manifest, encoding="utf-8")
    return load_bundle(destination)
