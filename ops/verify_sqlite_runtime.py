#!/usr/bin/env python3
from __future__ import annotations

import json
import platform
from pathlib import Path
import sqlite3
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

import _sqlite3

from repoctl.sqlite_runtime import sqlite_runtime_status


def build_report() -> dict[str, object]:
    with sqlite3.connect(":memory:") as connection:
        source_id = connection.execute("SELECT sqlite_source_id()").fetchone()[0]
    maps_path = Path("/proc/self/maps")
    loaded_libraries = (
        sorted(
            {
                line.split()[-1]
                for line in maps_path.read_text(encoding="utf-8").splitlines()
                if "libsqlite3" in line and "/" in line.split()[-1]
            }
        )
        if maps_path.is_file()
        else []
    )
    return {
        "python_executable": str(Path(sys.executable).resolve()),
        "python_version": platform.python_version(),
        "sqlite_extension": str(Path(_sqlite3.__file__).resolve()),
        "loaded_sqlite_libraries": loaded_libraries,
        "sqlite_source_id": source_id,
        **sqlite_runtime_status(),
    }


def main() -> int:
    report = build_report()
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["production_eligible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
