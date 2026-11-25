"""Disposable M011.004 sandbox probe; privileged execution remains operator-owned."""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
from typing import Any


CASES = ("missing", "parent", "separate")
PROTECTED = (
    "protected/wal/managed.sqlite3",
    "protected/wal/managed.sqlite3-wal",
    "protected/wal/managed.sqlite3-shm",
    "protected/clean-wal/managed.sqlite3",
    "protected/rollback/managed.sqlite3",
    "protected/state/sentinel",
)
GENERATED = (
    "prepared/output.sqlite3", "prepared/output.sqlite3.partial",
    "prepared/output.sqlite3.partial-wal", "prepared/output.sqlite3.partial-shm",
    "results/probe.json",
    "protected/clean-wal/managed.sqlite3-wal", "protected/clean-wal/managed.sqlite3-shm",
)
MARKER = "m011004-disposable-sandbox-v1"


def metadata(path: Path) -> dict[str, Any]:
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Fixture file is not regular: {path}")
    return {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "uid": info.st_uid, "gid": info.st_gid, "mode": oct(stat.S_IMODE(info.st_mode)),
    }


def validate(root: Path) -> dict[str, Any]:
    if not root.is_absolute() or root.resolve(strict=True) != root or not root.name.startswith("m011004-sandbox-"):
        raise ValueError("Only an absolute canonical m011004-sandbox-* fixture root is accepted.")
    owner = json.loads((root / "owner.json").read_text(encoding="utf-8"))
    if owner != {"marker": MARKER, "uid": os.getuid(), "gid": os.getgid()}:
        raise ValueError("Fixture ownership marker does not match the executing user.")
    return owner


def prepare(root: Path) -> dict[str, Any]:
    if not root.is_absolute() or not root.name.startswith("m011004-sandbox-"):
        raise ValueError("The fixture root must be absolute and named m011004-sandbox-*.")
    if root.parent.resolve(strict=True) != root.parent:
        raise ValueError("The fixture parent must be canonical.")
    root.mkdir(mode=0o700)
    owner = {"marker": MARKER, "uid": os.getuid(), "gid": os.getgid()}
    (root / "owner.json").write_text(json.dumps(owner), encoding="utf-8")
    (root / "owner.json").chmod(0o600)
    before: dict[str, Any] = {}
    for case in CASES:
        case_root = root / case
        for name in ("protected/wal", "protected/clean-wal", "protected/rollback", "protected/state", "prepared", "results"):
            (case_root / name).mkdir(parents=True, mode=0o700)
        seed = case_root / "prepared/seed.sqlite3"
        connection = sqlite3.connect(seed)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
            connection.execute("INSERT INTO sentinel VALUES ('fixture-only')")
            connection.commit()
            # Quiescent, test-owned bytes only. This is not a live-DB copy method.
            for suffix in ("", "-wal", "-shm"):
                shutil.copyfile(
                    seed.with_name(seed.name + suffix),
                    case_root / f"protected/wal/managed.sqlite3{suffix}",
                )
        finally:
            connection.close()
        seed.rename(case_root / "protected/clean-wal/managed.sqlite3")
        connection = sqlite3.connect(case_root / "protected/rollback/managed.sqlite3")
        try:
            connection.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
            connection.execute("INSERT INTO sentinel VALUES ('fixture-only')")
            connection.commit()
        finally:
            connection.close()
        (case_root / "protected/state/sentinel").write_bytes(b"protected fixture state\n")
        for relative in PROTECTED:
            path = case_root / relative
            path.chmod(0o600)
            before[f"{case}/{relative}"] = metadata(path)
    (root / "before.json").write_text(json.dumps(before, sort_keys=True), encoding="utf-8")
    (root / "before.json").chmod(0o600)
    return {"root": str(root), "owner": owner, "protected": before, "prepared_shm_exists": False}


def protected_status(root: Path, case: str | None = None) -> dict[str, Any]:
    before = json.loads((root / "before.json").read_text(encoding="utf-8"))
    if case is not None:
        before = {name: value for name, value in before.items() if name.startswith(f"{case}/")}
    after = {name: metadata(root / name) if (root / name).exists() else None for name in before}
    observed = {
        path.relative_to(root).as_posix()
        for selected in ((case,) if case is not None else CASES)
        for path in (root / selected / "protected").rglob("*") if not path.is_dir()
    }
    unexpected = sorted(observed - before.keys())
    return {
        "protected_unchanged": before == after and not unexpected,
        "protected": after, "unexpected_protected_paths": unexpected,
    }


def inspect(root: Path) -> dict[str, Any]:
    owner = validate(root)
    reports = {
        case: json.loads((root / case / "results/probe.json").read_text(encoding="utf-8"))
        for case in CASES if (root / case / "results/probe.json").exists()
    }
    return {"root": str(root), "owner": owner, **protected_status(root), "reports": reports}


def probe(root: Path, case: str) -> dict[str, Any]:
    owner = validate(root)
    if case not in CASES:
        raise ValueError("Unknown sandbox fixture case.")
    _owned_paths(root)
    status = Path("/proc/self/status").read_text(encoding="utf-8")
    if "NoNewPrivs:\t1" not in status:
        raise ValueError("Probe requires NoNewPrivileges; no fixture writes were attempted.")
    case_root = root / case
    result: dict[str, Any] = {
        "case": case, "uid": owner["uid"], "gid": owner["gid"],
        "no_new_privileges": True, "invocation_id": os.environ.get("INVOCATION_ID"),
        "python_started": True, "attempts": [], "sqlite": {},
    }
    if case == "missing":
        result["passed"] = False
        result["message"] = "Python unexpectedly started despite the required absent SHM mount."
    else:
        for source in ("wal", "clean-wal", "rollback"):
            path = case_root / "protected" / source / "managed.sqlite3"
            try:
                connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
                try:
                    rows = connection.execute("SELECT value FROM sentinel").fetchall()
                    result["sqlite"][source] = {"code": "READ_OK", "expected_rows": rows == [("fixture-only",)]}
                finally:
                    connection.close()
            except sqlite3.Error as exc:
                result["sqlite"][source] = {"code": "READ_REFUSED", "message": str(exc)}
        for relative in PROTECTED:
            path = case_root / relative
            original = path.read_bytes()
            for operation in ("write", "rename", "delete"):
                allowed = False
                failure = None
                try:
                    if operation == "write":
                        with path.open("ab") as stream:
                            stream.write(b"forbidden fixture write")
                        allowed = True
                        path.write_bytes(original)
                    elif operation == "rename":
                        renamed = path.with_name(path.name + ".probe-renamed")
                        path.rename(renamed)
                        allowed = True
                        renamed.rename(path)
                    else:
                        path.unlink()
                        allowed = True
                        path.write_bytes(original)
                        path.chmod(0o600)
                except OSError as exc:
                    failure = exc.errno
                result["attempts"].append({
                    "path": relative, "operation": operation, "allowed": allowed, "errno": failure,
                })
        sibling = case_root / "protected/sibling-created"
        try:
            sibling.write_bytes(b"must not be allowed")
            sibling.unlink()
            result["attempts"].append({"path": "protected/sibling-created", "operation": "create", "allowed": True, "errno": None})
        except OSError as exc:
            result["attempts"].append({"path": "protected/sibling-created", "operation": "create", "allowed": False, "errno": exc.errno})
        partial = case_root / "prepared/output.sqlite3.partial"
        connection = sqlite3.connect(partial)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE sentinel(value TEXT)")
            connection.execute("INSERT INTO sentinel VALUES ('prepared fixture')")
            connection.commit()
            result["prepared_sidecars_created"] = all(
                partial.with_name(partial.name + suffix).is_file() for suffix in ("-wal", "-shm")
            )
        finally:
            connection.close()
        partial.replace(case_root / "prepared/output.sqlite3")
        result["prepared_create_rename"] = True
        result["prepared_sidecars_removed"] = not any(
            partial.with_name(partial.name + suffix).exists() for suffix in ("-wal", "-shm")
        )
        result.update(protected_status(root, case))
        result["passed"] = (
            all(not attempt["allowed"] and attempt["errno"] in (errno.EROFS, errno.EBUSY) for attempt in result["attempts"])
            and all(result["sqlite"][source].get("expected_rows") for source in ("wal", "rollback"))
            and result["prepared_sidecars_created"]
            and result["prepared_sidecars_removed"]
            and result["protected_unchanged"]
        )
        result["message"] = "Fixture confinement passed." if result["passed"] else "Fixture confinement rejected; inspect allowed writes and SQLite results."
    report = case_root / "results/probe.json"
    report.write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
    report.chmod(0o600)
    return result


def _owned_paths(root: Path) -> list[Path]:
    allowed_files = {"owner.json", "before.json"} | {
        f"{case}/{relative}" for case in CASES for relative in PROTECTED + GENERATED
    }
    allowed_dirs = {
        parent.as_posix()
        for name in allowed_files for parent in Path(name).parents if parent != Path(".")
    }
    paths = list(root.rglob("*"))
    for path in paths:
        info = path.stat(follow_symlinks=False)
        name = path.relative_to(root).as_posix()
        if (
            path.is_symlink() or info.st_uid != os.getuid()
            or (path.is_file() and name not in allowed_files)
            or (path.is_dir() and name not in allowed_dirs)
            or not (path.is_file() or path.is_dir())
        ):
            raise ValueError(f"Unknown or unowned fixture entry; operation refused: {path}")
    return paths


def cleanup(root: Path) -> dict[str, Any]:
    validate(root)
    paths = _owned_paths(root)
    for path in sorted(paths, key=lambda item: len(item.parts), reverse=True):
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink()
    root.rmdir()
    return {"removed_only_fixture": str(root), "entry_count": len(paths)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "probe", "inspect", "cleanup"))
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--case", choices=CASES)
    arguments = parser.parse_args()
    if arguments.action == "probe":
        if arguments.case is None:
            parser.error("probe requires --case")
        report = probe(arguments.root, arguments.case)
    else:
        report = {"prepare": prepare, "inspect": inspect, "cleanup": cleanup}[arguments.action](arguments.root)
    print(json.dumps(report, sort_keys=True, indent=None if arguments.action == "probe" else 2))
    if report.get("passed") is False:
        raise SystemExit(1)
