from __future__ import annotations

import sqlite3
from typing import Any, Sequence


MINIMUM_SQLITE_VERSION = (3, 51, 3)


def sqlite_runtime_status(
    version_info: Sequence[int] | None = None,
    *,
    fixed_backport_evidence: str | None = None,
) -> dict[str, Any]:
    reported = tuple(sqlite3.sqlite_version_info if version_info is None else version_info)
    if len(reported) != 3 or any(type(part) is not int or part < 0 for part in reported):
        raise ValueError("SQLite version must contain three non-negative integers.")
    if fixed_backport_evidence is not None and (
        not isinstance(fixed_backport_evidence, str) or not fixed_backport_evidence.strip()
    ):
        raise ValueError("Fixed-backport evidence must be non-empty text when supplied.")
    eligible_by_version = reported >= MINIMUM_SQLITE_VERSION
    has_backport_evidence = fixed_backport_evidence is not None
    return {
        "sqlite_version_info": reported,
        "sqlite_version": ".".join(str(part) for part in reported),
        "minimum_required": MINIMUM_SQLITE_VERSION,
        "production_eligible": eligible_by_version,
        "eligibility_basis": (
            "minimum_version"
            if eligible_by_version
            else "backport_evidence_unverified"
            if has_backport_evidence
            else "below_minimum"
        ),
        "fixed_backport_evidence": (
            fixed_backport_evidence.strip() if fixed_backport_evidence is not None else None
        ),
        "fixed_backport_evidence_status": "unverified" if has_backport_evidence else "not_provided",
    }
