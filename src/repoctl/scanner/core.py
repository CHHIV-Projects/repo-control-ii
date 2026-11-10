from __future__ import annotations

from contextlib import contextmanager
import errno
import fcntl
import hashlib
import os
import shutil
import stat
import threading
import time
from pathlib import Path
from tempfile import mkdtemp
from typing import Any, Iterator

from .git_ops import ScanError, get_branch, get_head_commit, get_working_tree, list_tracked_files, validate_git_worktree
from .python_scan import discover_test_structure, is_test_like_python_path, parse_python_file
from .relationships import build_relationships
from .summary import build_summary
from .util import classify_file_type, count_lines_if_text, encode_path_order, make_repository_id, write_json_deterministic


SCHEMA_VERSION = 1
DEFAULT_STATE_ROOT = Path("~/.local/share/repoctl").expanduser()
STATE_ARTIFACT_LOCK = threading.RLock()
STATE_ARTIFACT_LOCK_TIMEOUT_SECONDS = 10.0
_STATE_ARTIFACT_LOCK_LOCAL = threading.local()


class StateArtifactLockError(RuntimeError):
    pass


class StateArtifactLockBusyError(StateArtifactLockError):
    pass


class StateArtifactLockOrderError(StateArtifactLockError):
    pass


def state_artifact_lock_held() -> bool:
    return bool(getattr(_STATE_ARTIFACT_LOCK_LOCAL, "held", None))


def _open_state_artifact_lock(state_root: Path) -> tuple[int, int]:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        root_descriptor = os.open(state_root, directory_flags)
    except OSError as exc:
        raise StateArtifactLockError(f"Could not safely open state root: {exc}") from exc
    try:
        try:
            os.mkdir(".locks", mode=0o700, dir_fd=root_descriptor)
        except FileExistsError:
            pass
        try:
            lock_directory_descriptor = os.open(".locks", directory_flags, dir_fd=root_descriptor)
        except OSError as exc:
            raise StateArtifactLockError(f"Could not safely open state lock directory: {exc}") from exc
    finally:
        os.close(root_descriptor)

    try:
        directory_info = os.fstat(lock_directory_descriptor)
        if (
            not stat.S_ISDIR(directory_info.st_mode)
            or directory_info.st_uid != os.geteuid()
            or directory_info.st_mode & 0o077
        ):
            raise StateArtifactLockError("State lock directory has unsafe type, ownership, or permissions.")
        try:
            lock_descriptor = os.open(
                "state-artifacts.lock",
                os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=lock_directory_descriptor,
            )
        except OSError as exc:
            raise StateArtifactLockError(f"Could not safely open persistent state lock file: {exc}") from exc
        try:
            lock_info = os.fstat(lock_descriptor)
            named_info = os.stat(
                "state-artifacts.lock",
                dir_fd=lock_directory_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(lock_info.st_mode)
                or lock_info.st_uid != os.geteuid()
                or lock_info.st_mode & 0o077
                or lock_info.st_nlink != 1
                or (lock_info.st_dev, lock_info.st_ino) != (named_info.st_dev, named_info.st_ino)
            ):
                raise StateArtifactLockError("Persistent state lock file has unsafe identity or permissions.")
            return lock_descriptor, lock_directory_descriptor
        except BaseException:
            os.close(lock_descriptor)
            raise
    except BaseException:
        os.close(lock_directory_descriptor)
        raise


def _verify_state_artifact_lock_path(lock_descriptor: int, directory_descriptor: int) -> None:
    try:
        lock_info = os.fstat(lock_descriptor)
        named_info = os.stat(
            "state-artifacts.lock",
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise StateArtifactLockError(f"Persistent state lock file changed or became unavailable: {exc}") from exc
    if (
        not stat.S_ISREG(named_info.st_mode)
        or (lock_info.st_dev, lock_info.st_ino) != (named_info.st_dev, named_info.st_ino)
    ):
        raise StateArtifactLockError("Persistent state lock file was replaced while in use.")


@contextmanager
def state_artifact_lock(state_root: str | Path) -> Iterator[None]:
    root = Path(state_root).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        canonical_root = root.resolve(strict=True)
    except OSError as exc:
        raise StateArtifactLockError(f"Could not resolve canonical state root: {exc}") from exc
    held = getattr(_STATE_ARTIFACT_LOCK_LOCAL, "held", None)
    if held:
        if canonical_root not in held:
            raise StateArtifactLockOrderError(
                "Nested state-artifact locks must use the same canonical state root."
            )
        held[canonical_root]["depth"] += 1
        try:
            yield
        finally:
            held[canonical_root]["depth"] -= 1
        return

    deadline = time.monotonic() + STATE_ARTIFACT_LOCK_TIMEOUT_SECONDS
    if not STATE_ARTIFACT_LOCK.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise StateArtifactLockBusyError(
            f"State artifact lock is busy after {STATE_ARTIFACT_LOCK_TIMEOUT_SECONDS:g} seconds."
        )
    try:
        held = getattr(_STATE_ARTIFACT_LOCK_LOCAL, "held", None)
        if held:
            raise StateArtifactLockOrderError("State-artifact lock order changed during acquisition.")
        descriptor, directory_descriptor = _open_state_artifact_lock(canonical_root)
        acquired = False
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise StateArtifactLockError(f"Could not acquire persistent state lock: {exc}") from exc
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise StateArtifactLockBusyError(
                            f"State artifact lock is busy after {STATE_ARTIFACT_LOCK_TIMEOUT_SECONDS:g} seconds."
                        ) from exc
                    time.sleep(min(0.01, remaining))
            _verify_state_artifact_lock_path(descriptor, directory_descriptor)
            _STATE_ARTIFACT_LOCK_LOCAL.held = {canonical_root: {"depth": 1}}
            try:
                yield
            finally:
                del _STATE_ARTIFACT_LOCK_LOCAL.held
                _verify_state_artifact_lock_path(descriptor, directory_descriptor)
        finally:
            try:
                if acquired:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    except OSError as exc:
                        raise StateArtifactLockError(f"Could not release persistent state lock: {exc}") from exc
            finally:
                os.close(descriptor)
                os.close(directory_descriptor)
    finally:
        STATE_ARTIFACT_LOCK.release()


def _extract_requirements(repo_root: Path, tracked_files: list[str]) -> list[dict[str, Any]]:
    req_files = [p for p in tracked_files if Path(p).name == "requirements.txt"]
    req_files.sort(key=encode_path_order)

    results: list[dict[str, Any]] = []
    for rel_path in req_files:
        full_path = repo_root / rel_path
        try:
            text = full_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ScanError(f"requirements file is not utf-8 decodable: {rel_path}: {exc}") from exc

        declarations: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                continue
            declarations.append(stripped)

        results.append({"path": rel_path, "declarations": declarations})

    return results


def _build_files_payload(repo_root: Path, tracked_files: list[str]) -> dict[str, Any]:
    records = []
    for rel_path in sorted(tracked_files, key=encode_path_order):
        file_bytes = (repo_root / rel_path).read_bytes()
        records.append(
            {
                "path": rel_path,
                "type": classify_file_type(rel_path),
                "byte_size": len(file_bytes),
                "line_count": count_lines_if_text(file_bytes),
                "sha256": hashlib.sha256(file_bytes).hexdigest(),
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "files": records,
    }


def _build_symbols_payload(repo_root: Path, tracked_files: list[str]) -> dict[str, Any]:
    python_files = [p for p in tracked_files if p.endswith(".py")]
    python_files.sort(key=encode_path_order)

    records = []
    for rel_path in python_files:
        parsed = parse_python_file(repo_root, rel_path)
        records.append(
            {
                "path": parsed.path,
                "parse_success": parsed.parse_success,
                "parse_error": parsed.parse_error,
                "top_level_functions": parsed.top_level_functions,
                "top_level_async_functions": parsed.top_level_async_functions,
                "top_level_classes": parsed.top_level_classes,
                "imports": parsed.imports,
                "imported_module_names": parsed.imported_module_names,
                "imported_symbols": parsed.imported_symbols,
                "source_evidence": parsed.source_evidence,
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "python_files": records,
    }


def _build_tests_payload(repo_root: Path, tracked_files: list[str]) -> dict[str, Any]:
    test_files = [p for p in tracked_files if is_test_like_python_path(p)]
    test_files.sort(key=encode_path_order)

    records = []
    class_count = 0
    method_count = 0
    function_count = 0

    for rel_path in test_files:
        discovered = discover_test_structure(repo_root, rel_path)
        records.append(discovered)
        class_count += len(discovered["classes"])
        method_count += sum(len(c["test_methods"]) for c in discovered["classes"])
        function_count += len(discovered["top_level_test_functions"])

    return {
        "schema_version": SCHEMA_VERSION,
        "test_discovery_convention": {
            "python_file_patterns": ["**/test_*.py", "**/*_test.py", "**/tests/**/*.py"],
            "test_name_rule": "name starts with 'test'",
        },
        "test_like_file_count": len(test_files),
        "test_class_count": class_count,
        "test_method_count": method_count,
        "top_level_test_function_count": function_count,
        "test_files": records,
    }


def _build_repository_payload(
    repo_root: Path,
    repository_id: str,
    branch: dict[str, Any],
    head_commit: str,
    working_tree: dict[str, Any],
    tracked_count: int,
    requirements: list[dict[str, Any]],
) -> dict[str, Any]:
    categories = {
        "modified": [],
        "added_or_staged": [],
        "deleted": [],
        "renamed": [],
        "untracked": [],
        "other": [],
    }

    for entry in working_tree["entries"]:
        if entry["kind"] == "untracked":
            categories["untracked"].append(entry["path"])
            continue

        xy = entry.get("xy") or ""
        if entry["kind"] == "rename_or_copy":
            categories["renamed"].append(entry["path"])
        if "M" in xy:
            categories["modified"].append(entry["path"])
        elif "A" in xy:
            categories["added_or_staged"].append(entry["path"])
        elif "D" in xy:
            categories["deleted"].append(entry["path"])
        elif not xy:
            categories["other"].append(entry["path"])
        else:
            if entry["path"] not in categories["renamed"]:
                categories["other"].append(entry["path"])

    for key in categories:
        categories[key].sort(key=encode_path_order)

    return {
        "schema_version": SCHEMA_VERSION,
        "repository_root": str(repo_root),
        "repository_id": repository_id,
        "branch": branch,
        "head_commit": head_commit,
        "working_tree": working_tree,
        "working_tree_categories": categories,
        "tracked_file_count": tracked_count,
        "requirements": requirements,
    }


def run_scan(repository_path: str, state_root: Path | None = None) -> dict[str, Any]:
    target_path = Path(repository_path).expanduser().resolve()
    repo_root = validate_git_worktree(target_path)

    branch = get_branch(repo_root)
    head_commit = get_head_commit(repo_root)
    tracked_files = list_tracked_files(repo_root)
    working_tree = get_working_tree(repo_root)
    repository_id = make_repository_id(repo_root)
    requirements = _extract_requirements(repo_root, tracked_files)

    repository_payload = _build_repository_payload(
        repo_root=repo_root,
        repository_id=repository_id,
        branch=branch,
        head_commit=head_commit,
        working_tree=working_tree,
        tracked_count=len(tracked_files),
        requirements=requirements,
    )
    files_payload = _build_files_payload(repo_root, tracked_files)
    symbols_payload = _build_symbols_payload(repo_root, tracked_files)
    tests_payload_v1 = _build_tests_payload(repo_root, tracked_files)
    dependencies_payload, tests_payload = build_relationships(
        repo_root=repo_root,
        tracked_files=tracked_files,
        symbols_payload=symbols_payload,
        tests_payload_v1=tests_payload_v1,
    )

    summary_text = build_summary(
        repository=repository_payload,
        files_payload=files_payload,
        symbols_payload=symbols_payload,
        tests_payload=tests_payload,
        dependencies_payload=dependencies_payload,
    )

    root = (state_root or DEFAULT_STATE_ROOT).expanduser().resolve()
    final_dir = root / repository_id
    with state_artifact_lock(root):
        root.mkdir(parents=True, exist_ok=True)
        backup_dir = root / f"{repository_id}.bak"
        if backup_dir.exists() or backup_dir.is_symlink():
            raise ScanError(
                f"An existing scanner backup requires operator review; refusing to replace it: {backup_dir.name}"
            )
        if final_dir.is_symlink() or (final_dir.exists() and not final_dir.is_dir()):
            raise ScanError("Existing repository state is not a regular directory.")

        temp_dir = Path(mkdtemp(prefix="scan-tmp-", dir=str(root)))
        try:
            write_json_deterministic(temp_dir / "repository.json", repository_payload)
            write_json_deterministic(temp_dir / "files.json", files_payload)
            write_json_deterministic(temp_dir / "symbols.json", symbols_payload)
            write_json_deterministic(temp_dir / "tests.json", tests_payload)
            write_json_deterministic(temp_dir / "dependencies.json", dependencies_payload)
            (temp_dir / "summary.md").write_text(summary_text, encoding="utf-8", newline="\n")

            # Keep previously generated higher-level state stable across scans.
            for namespace in ("contexts", "snapshots", "comparisons", "analyses", "workflow", "milestones"):
                existing_dir = final_dir / namespace
                if existing_dir.exists() and existing_dir.is_dir():
                    shutil.copytree(existing_dir, temp_dir / namespace)

            from ..workflow.architect_dispatches import (
                ArchitectDispatchError,
                _preserve_dispatch_ledger,
            )

            try:
                _preserve_dispatch_ledger(final_dir, temp_dir, repository_id)
            except ArchitectDispatchError as exc:
                raise ScanError(f"Cannot safely preserve Architect dispatch state: {exc}") from exc

            if final_dir.exists():
                final_dir.rename(backup_dir)

            try:
                temp_dir.rename(final_dir)
            except Exception:
                if backup_dir.exists() and not final_dir.exists():
                    try:
                        backup_dir.rename(final_dir)
                    except OSError as restore_error:
                        raise ScanError(
                            f"Scanner publication failed and the prior state could not be restored; "
                            f"recoverable state remains at {backup_dir.name}: {restore_error}"
                        ) from restore_error
                raise

            if backup_dir.exists():
                shutil.rmtree(backup_dir)
        except Exception:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
            raise

    parse_error_count = sum(1 for item in symbols_payload["python_files"] if not item["parse_success"])

    return {
        "repository_root": str(repo_root),
        "repository_id": repository_id,
        "head_commit": head_commit,
        "branch": branch,
        "output_dir": str(final_dir),
        "parse_error_count": parse_error_count,
        "repository_payload": repository_payload,
        "files_payload": files_payload,
        "symbols_payload": symbols_payload,
        "tests_payload": tests_payload,
        "dependencies_payload": dependencies_payload,
        "summary_text": summary_text,
    }


def run_scan_with_artifacts(repository_path: str, state_root: Path | None = None) -> dict[str, Any]:
    return run_scan(repository_path=repository_path, state_root=state_root)
