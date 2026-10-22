from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import json
import math
from pathlib import Path
import sqlite3
import statistics
from typing import Any, Iterator

from .provider import IntelligenceError

RATINGS = ("USEFUL", "PARTIAL", "NO_VALUE", "MISLEADING", "UNSUPPORTED")
MAX_USES = 10000
MAX_REFRESHES = 10000


@contextmanager
def database(root: Path) -> Iterator[sqlite3.Connection]:
    path = root / "measurements.sqlite3"
    if path.is_symlink():
        raise IntelligenceError("Unsafe measurement database path.")
    connection = None
    try:
        connection = sqlite3.connect(path, timeout=5)
        with connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA max_page_count=16384")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS uses (
                id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, repository_key TEXT NOT NULL,
                generation TEXT, consumer TEXT NOT NULL, operation TEXT NOT NULL,
                latency_ms REAL NOT NULL, response_bytes INTEGER NOT NULL,
                nodes INTEGER NOT NULL, relationships INTEGER NOT NULL, outcome TEXT NOT NULL,
                truncated INTEGER NOT NULL, rating TEXT, inspected INTEGER,
                misleading_ever INTEGER NOT NULL DEFAULT 0)"""
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS benchmarks (id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS refreshes (
                id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, repository_key TEXT NOT NULL,
                repository_id TEXT NOT NULL, actor TEXT NOT NULL, registry_generation INTEGER,
                milestone_id TEXT, triggering_freshness TEXT NOT NULL, action TEXT NOT NULL,
                generation_before TEXT, generation_after TEXT, source_identity_before TEXT,
                source_identity_after TEXT, final_freshness TEXT NOT NULL, elapsed_ms REAL NOT NULL,
                error_code TEXT, completed INTEGER NOT NULL)"""
            )
            yield connection
    except sqlite3.Error as exc:
        raise IntelligenceError(f"Intelligence measurements could not be persisted/read: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()
    path.chmod(0o600)


def record(root: Path, row: dict[str, Any]) -> None:
    with database(root) as db:
        if db.execute("SELECT COUNT(*) FROM uses").fetchone()[0] >= MAX_USES:
            raise IntelligenceError("Intelligence measurement capacity reached; archive/review before further uses.")
        db.execute(
            """INSERT INTO uses
            (id,timestamp,repository_key,generation,consumer,operation,latency_ms,response_bytes,
             nodes,relationships,outcome,truncated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(row[key] for key in (
                "id", "timestamp", "repository_key", "generation", "consumer", "operation",
                "latency_ms", "response_bytes", "nodes", "relationships", "outcome", "truncated",
            )),
        )


def rate(root: Path, use_id: str, rating: str, inspected: bool | None = None) -> None:
    if (not isinstance(use_id, str) or not 1 <= len(use_id) <= 64
        or not isinstance(rating, str) or rating not in RATINGS
        or inspected is not None and not isinstance(inspected, bool)):
        raise IntelligenceError("Invalid usefulness rating or inspection flag.")
    with database(root) as db:
        updated = db.execute(
            """UPDATE uses SET rating=?, inspected=COALESCE(?,inspected),
            misleading_ever=MAX(misleading_ever,?) WHERE id=?""",
            (rating, inspected, rating == "MISLEADING", use_id),
        )
        if updated.rowcount != 1:
            raise IntelligenceError("Intelligence use not found in this repository.")


def add_benchmark(root: Path, payload: dict[str, Any]) -> None:
    if set(payload) != {"id", "task", "control", "graphify", "assessment", "method"}:
        raise IntelligenceError("Benchmark requires id/task/control/graphify/assessment/method.")
    for key, limit in (("id", 64), ("task", 240), ("assessment", 1200), ("method", 1200)):
        if not isinstance(payload[key], str) or not 0 < len(payload[key]) <= limit:
            raise IntelligenceError(f"Invalid benchmark {key}.")
    metrics = {"files", "source_bytes", "evidence_bytes", "interactions", "elapsed_ms"}
    for arm in ("control", "graphify"):
        if not isinstance(payload[arm], dict) or set(payload[arm]) != metrics:
            raise IntelligenceError("Benchmark arms require files/source_bytes/evidence_bytes/interactions/elapsed_ms.")
        for value in payload[arm].values():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise IntelligenceError("Benchmark metrics must be finite non-negative numbers.")
    with database(root) as db:
        if db.execute("SELECT COUNT(*) FROM benchmarks").fetchone()[0] >= 100:
            raise IntelligenceError("Benchmark capacity reached.")
        db.execute("INSERT INTO benchmarks VALUES (?,?)", (payload["id"], json.dumps(payload)))


def begin_refresh(root: Path, row: dict[str, Any]) -> None:
    required = {
        "id", "timestamp", "repository_key", "repository_id", "actor", "registry_generation",
        "milestone_id", "triggering_freshness", "action", "generation_before", "generation_after",
        "source_identity_before", "source_identity_after", "final_freshness", "elapsed_ms",
        "error_code", "completed",
    }
    if set(row) != required or row["completed"] is not False:
        raise IntelligenceError("Invalid intelligence refresh receipt.")
    with database(root) as db:
        if db.execute("SELECT COUNT(*) FROM refreshes").fetchone()[0] >= MAX_REFRESHES:
            raise IntelligenceError("Intelligence refresh receipt capacity reached; review/archive before further refreshes.")
        db.execute(
            """INSERT INTO refreshes
            (id,timestamp,repository_key,repository_id,actor,registry_generation,milestone_id,
             triggering_freshness,action,generation_before,generation_after,source_identity_before,
             source_identity_after,final_freshness,elapsed_ms,error_code,completed)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(row[key] for key in (
                "id", "timestamp", "repository_key", "repository_id", "actor", "registry_generation",
                "milestone_id", "triggering_freshness", "action", "generation_before", "generation_after",
                "source_identity_before", "source_identity_after", "final_freshness", "elapsed_ms",
                "error_code", "completed",
            )),
        )


def finish_refresh(root: Path, row: dict[str, Any]) -> None:
    with database(root) as db:
        updated = db.execute(
            """UPDATE refreshes SET action=?,generation_after=?,source_identity_before=?,
            source_identity_after=?,final_freshness=?,elapsed_ms=?,error_code=?,completed=?
            WHERE id=?""",
            tuple(row[key] for key in (
                "action", "generation_after", "source_identity_before", "source_identity_after",
                "final_freshness", "elapsed_ms", "error_code", "completed", "id",
            )),
        )
        if updated.rowcount != 1:
            raise IntelligenceError("Intelligence refresh receipt could not be finalized.")


def summary(root: Path) -> dict[str, Any]:
    with database(root) as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM uses ORDER BY timestamp DESC,id DESC")]
        benchmarks = [json.loads(row[0]) for row in db.execute("SELECT payload FROM benchmarks ORDER BY id")]
        refreshes = [
            dict(row)
            for row in db.execute("SELECT * FROM refreshes ORDER BY timestamp DESC,id DESC LIMIT 30")
        ]
        refresh_attempts = db.execute("SELECT COUNT(*) FROM refreshes").fetchone()[0]
        completed_refreshes = db.execute(
            "SELECT COUNT(*) FROM refreshes WHERE completed=1"
        ).fetchone()[0]
    counts = Counter(row["rating"] for row in rows if row["rating"])
    rated = sum(counts.values())
    latencies = sorted(row["latency_ms"] for row in rows)
    operations = []
    for operation in sorted({row["operation"] for row in rows}):
        group = [row for row in rows if row["operation"] == operation]
        operations.append({
            "operation": operation, "uses": len(group),
            "ratings": dict(Counter(row["rating"] or "UNRATED" for row in group)),
            "outcomes": dict(Counter(row["outcome"] for row in group)),
            "misleading_ever": sum(row["misleading_ever"] for row in group),
        })
    return {
        "uses": len(rows), "rated": rated, "unrated": len(rows) - rated,
        "ratings": [
            {"rating": rating, "count": counts[rating], "percent": 100 * counts[rating] / rated if rated else 0}
            for rating in RATINGS
        ],
        "median_latency_ms": statistics.median(latencies) if rows else None,
        "p95_latency_ms": latencies[math.ceil(len(rows) * .95) - 1] if rows else None,
        "median_response_bytes": statistics.median(row["response_bytes"] for row in rows) if rows else None,
        "truncated": sum(row["truncated"] for row in rows),
        "ambiguous": sum(row["outcome"] == "AMBIGUOUS" for row in rows),
        "misleading_ever": sum(row["misleading_ever"] for row in rows),
        "misleading_uses": [row for row in rows if row["misleading_ever"]][:100],
        "omitted_misleading_uses": max(0, sum(row["misleading_ever"] for row in rows) - 100),
        "operations": operations, "recent": rows[:30], "benchmarks": benchmarks,
        "refresh_attempts": refresh_attempts,
        "completed_refreshes": completed_refreshes,
        "refreshes": refreshes,
    }
