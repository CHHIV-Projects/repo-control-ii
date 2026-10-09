from __future__ import annotations

import os
import selectors
import subprocess
import time
from pathlib import Path
from typing import Any


class ScanError(RuntimeError):
    pass


def _run_git_bounded(
    repo_root: Path,
    args: list[str],
    *,
    stdout_limit: int,
    stderr_limit: int = 16 * 1024,
    timeout_seconds: float = 10,
    env_overrides: dict[str, str] | None = None,
) -> tuple[bytes, bytes, int, bool]:
    try:
        env = os.environ.copy()
        env["GIT_OPTIONAL_LOCKS"] = "0"
        env["GIT_TERMINAL_PROMPT"] = "0"
        if env_overrides:
            env.update(env_overrides)
        proc = subprocess.Popen(
            ["git", "-C", str(repo_root), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            bufsize=0,
        )
    except OSError as exc:
        raise ScanError(f"unable to start git command: {exc}") from exc

    assert proc.stdout is not None
    assert proc.stderr is not None
    output = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {"stdout": stdout_limit, "stderr": stderr_limit}
    truncated = False
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
    selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
    deadline = time.monotonic() + timeout_seconds

    try:
        for key in (proc.stdout, proc.stderr):
            os.set_blocking(key.fileno(), False)

        while selector.get_map():
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                proc.kill()
                proc.wait()
                raise ScanError(f"git command timed out after {timeout_seconds:g} seconds")

            for key, _ in selector.select(min(remaining_time, 0.1)):
                stream_name = key.data
                remaining_bytes = limits[stream_name] - len(output[stream_name])
                chunk = os.read(key.fd, min(65536, remaining_bytes + 1))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue

                accepted = chunk[: max(remaining_bytes, 0)]
                output[stream_name].extend(accepted)
                if len(chunk) > len(accepted):
                    truncated = True
                    proc.kill()
                    break

            if truncated:
                break

        if truncated:
            for key in list(selector.get_map().values()):
                selector.unregister(key.fileobj)
            proc.stdout.close()
            proc.stderr.close()
        returncode = proc.wait()
    finally:
        selector.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()

    return bytes(output["stdout"]), bytes(output["stderr"]), returncode, truncated


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
            text = chunk.decode("utf-8", errors="surrogateescape")
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
            text = chunk.decode("utf-8", errors="surrogateescape")
            fields = text.split(" ", 9)
            if len(fields) != 10:
                raise ScanError(f"unsupported porcelain v2 rename/copy record: {text}")
            _, xy, sub, _mH, _mI, _mW, _hH, _hI, xscore, path = fields
            if i + 1 >= len(chunks):
                raise ScanError(f"rename/copy record missing original path: {text}")
            original_path = os.fsdecode(chunks[i + 1])
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
            text = chunk.decode("utf-8", errors="surrogateescape")
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
            path = os.fsdecode(chunk[2:])
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

    entries.sort(key=lambda e: (os.fsencode(e["path"]), os.fsencode(e["original_path"] or "")))
    return {"headers": headers, "entries": entries}


def get_working_tree(repo_root: Path) -> dict[str, Any]:
    output = _run_git_bytes(repo_root, ["status", "--porcelain=v2", "-z", "--untracked-files=all"])
    chunks = [c for c in output.split(b"\x00") if c]
    parsed = _parse_porcelain_v2_chunks(chunks, include_headers=False)

    return {
        "is_clean": len(parsed["entries"]) == 0,
        "entries": parsed["entries"],
    }


def get_working_tree_with_branch(
    repo_root: Path,
    *,
    max_output_bytes: int | None = None,
) -> dict[str, Any]:
    args = ["status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all"]
    if max_output_bytes is None:
        output = _run_git_bytes(repo_root, args)
    else:
        output, stderr, returncode, truncated = _run_git_bounded(
            repo_root,
            args,
            stdout_limit=max_output_bytes,
        )
        if truncated:
            raise ScanError("git status output exceeded the configured limit; repository state is incomplete")
        if returncode != 0:
            message = stderr.decode("utf-8", errors="replace").strip()
            raise ScanError(f"git command failed: status: {message}")
    chunks = [c for c in output.split(b"\x00") if c]
    parsed = _parse_porcelain_v2_chunks(chunks, include_headers=True)

    return {
        "is_clean": len(parsed["entries"]) == 0,
        "headers": parsed["headers"],
        "entries": parsed["entries"],
    }
