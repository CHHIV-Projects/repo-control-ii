#!/usr/bin/env python3
"""Verify or undo only the M010.031 historical status-reporting correction."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile


REPOSITORY = Path(__file__).resolve().parents[2]
RELATIVE_PATH = "src/repoctl/workflow/coder_interface.py"
PRIOR_SHA256 = "5bf6ba4f354704304b88471ac823dc5051cbcff422fba76da029bf7543778452"
CORRECTED_SHA256 = "cac90db3596456b95f0741e29c6ebb7618e10a8c87aace3b06c1d6ec7717a0a5"


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def prior_content() -> bytes:
    result = subprocess.run(
        ["git", "show", f"HEAD:{RELATIVE_PATH}"],
        cwd=REPOSITORY,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0 or digest(result.stdout) != PRIOR_SHA256:
        raise RuntimeError("HEAD no longer contains the exact pre-correction source image")
    return result.stdout


def verify() -> None:
    current = (REPOSITORY / RELATIVE_PATH).read_bytes()
    if digest(current) != CORRECTED_SHA256:
        raise RuntimeError("current historical status source hash is not the reviewed correction")
    prior_content()
    print("PASS exact historical status correction and code-only rollback source")


def rollback() -> None:
    path = REPOSITORY / RELATIVE_PATH
    current = path.read_bytes()
    if digest(current) != CORRECTED_SHA256:
        raise RuntimeError("refusing rollback from an unrecognized source image")
    prior = prior_content()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), path.stat().st_mode & 0o777)
            stream.write(prior)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary_path.unlink(missing_ok=True)
    if digest(path.read_bytes()) != PRIOR_SHA256:
        raise RuntimeError("post-rollback source hash mismatch")
    print("CODE ROLLBACK VERIFIED: historical status correction removed; SQLite and service unchanged")


def main() -> int:
    action = sys.argv[1] if len(sys.argv) == 2 else ""
    if action == "verify":
        verify()
    elif action == "rollback":
        rollback()
    else:
        print(f"Usage: {Path(sys.argv[0]).name} {{verify|rollback}}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
