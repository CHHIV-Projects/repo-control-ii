from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


class ScanError(RuntimeError):
    pass


def _run_git(repo_root: Path, args: list[str]) -> str:
    cmd = ["git", "-C", str(repo_root), *args]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise ScanError(f"git command failed: {' '.join(args)}: {stderr}")
    return proc.stdout.decode("utf-8", errors="strict")


def _run_git_bytes(repo_root: Path, args: list[str]) -> bytes:
    cmd = ["git", "-C", str(repo_root), *args]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise ScanError(f"git command failed: {' '.join(args)}: {stderr}")
    return proc.stdout


def validate_git_worktree(target_path: Path) -> Path:
    if not target_path.exists():
        raise ScanError(f"target path does not exist: {target_path}")

    try:
        top_level = _run_git(target_path, ["rev-parse", "--show-toplevel"]).strip()
    except ScanError as exc:
        raise ScanError(f"not a git work tree: {target_path}") from exc

    return Path(top_level).resolve(strict=True)


def get_head_commit(repo_root: Path) -> str:
    return _run_git(repo_root, ["rev-parse", "HEAD"]).strip()


def get_branch(repo_root: Path) -> dict[str, Any]:
    branch_name = _run_git(repo_root, ["branch", "--show-current"]).strip()
    if branch_name:
        return {"state": "attached", "name": branch_name}
    return {"state": "detached", "name": None}


def list_tracked_files(repo_root: Path) -> list[str]:
    output = _run_git_bytes(repo_root, ["ls-files", "-z"])
    parts = output.split(b"\x00")
    files = [p.decode("utf-8", errors="surrogateescape") for p in parts if p]
    return files


def _parse_porcelain_v2_chunks(chunks: list[bytes], *, include_headers: bool) -> dict[str, Any]:
    headers: list[str] = []
    entries: list[dict[str, Any]] = []
    i = 0
    while i < len(chunks):
        chunk = chunks[i]

        if chunk.startswith(b"# "):
            if not include_headers:
                text = chunk.decode("utf-8", errors="replace")
                raise ScanError(f"unsupported porcelain v2 record type: {text}")
            headers.append(chunk.decode("utf-8", errors="strict"))
            i += 1
            continue

        if chunk.startswith(b"1 "):
            text = chunk.decode("utf-8", errors="strict")
            fields = text.split(" ", 8)
            if len(fields) != 9:
                raise ScanError(f"unsupported porcelain v2 ordinary record: {text}")
            _, xy, sub, _mH, _mI, _mW, _hH, _hI, path = fields
            entries.append(
                {
                    "kind": "ordinary",
                    "path": path,
                    "original_path": None,
                    "xy": xy,
                    "submodule": sub,
                    "operation": None,
                    "similarity": None,
                }
            )
            i += 1
            continue

        if chunk.startswith(b"2 "):
            text = chunk.decode("utf-8", errors="strict")
            fields = text.split(" ", 9)
            if len(fields) != 10:
                raise ScanError(f"unsupported porcelain v2 rename/copy record: {text}")
            _, xy, sub, _mH, _mI, _mW, _hH, _hI, xscore, path = fields
            if i + 1 >= len(chunks):
                raise ScanError(f"rename/copy record missing original path: {text}")
            original_path = chunks[i + 1].decode("utf-8", errors="strict")
            if not xscore or xscore[0] not in ("R", "C"):
                raise ScanError(f"unsupported rename/copy operation marker: {xscore}")
            similarity = xscore[1:] if len(xscore) > 1 else None
            entries.append(
                {
                    "kind": "rename_or_copy",
                    "path": path,
                    "original_path": original_path,
                    "xy": xy,
                    "submodule": sub,
                    "operation": "rename" if xscore[0] == "R" else "copy",
                    "similarity": similarity,
                }
            )
            i += 2
            continue

        if chunk.startswith(b"u "):
            text = chunk.decode("utf-8", errors="strict")
            fields = text.split(" ", 10)
            if len(fields) != 11:
                raise ScanError(f"unsupported porcelain v2 unmerged record: {text}")
            _, xy, sub, _m1, _m2, _m3, _m4, _h1, _h2, _h3, path = fields
            entries.append(
                {
                    "kind": "unmerged",
                    "path": path,
                    "original_path": None,
                    "xy": xy,
                    "submodule": sub,
                    "operation": None,
                    "similarity": None,
                }
            )
            i += 1
            continue

        if chunk.startswith(b"? "):
            text = chunk.decode("utf-8", errors="strict")
            path = text[2:]
            entries.append(
                {
                    "kind": "untracked",
                    "path": path,
                    "original_path": None,
                    "xy": None,
                    "submodule": None,
                    "operation": None,
                    "similarity": None,
                }
            )
            i += 1
            continue

        prefix = chunk[:1].decode("utf-8", errors="replace")
        raise ScanError(f"unsupported porcelain v2 record type: {prefix}")

    entries.sort(key=lambda e: (e["path"].encode(), (e["original_path"] or "").encode()))
    return {"headers": headers, "entries": entries}


def get_working_tree(repo_root: Path) -> dict[str, Any]:
    output = _run_git_bytes(repo_root, ["status", "--porcelain=v2", "-z", "--untracked-files=all"])
    chunks = [c for c in output.split(b"\x00") if c]
    parsed = _parse_porcelain_v2_chunks(chunks, include_headers=False)

    return {
        "is_clean": len(parsed["entries"]) == 0,
        "entries": parsed["entries"],
    }


def get_working_tree_with_branch(repo_root: Path) -> dict[str, Any]:
    output = _run_git_bytes(repo_root, ["status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all"])
    chunks = [c for c in output.split(b"\x00") if c]
    parsed = _parse_porcelain_v2_chunks(chunks, include_headers=True)

    return {
        "is_clean": len(parsed["entries"]) == 0,
        "headers": parsed["headers"],
        "entries": parsed["entries"],
    }
