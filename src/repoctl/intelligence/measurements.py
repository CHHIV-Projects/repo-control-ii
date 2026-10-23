from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import statistics
from typing import Any, Iterator

from .provider import IntelligenceError

RATINGS = ("USEFUL", "PARTIAL", "NO_VALUE", "MISLEADING", "UNSUPPORTED")
MAX_USES = 10000
MAX_REFRESHES = 10000
CHAR4_ESTIMATOR_ID = "char4-ceil-v1"
CHAR4_ESTIMATOR_VERSION = "1"
NORMAL_EVIDENCE_SURFACE = "bounded-normalized-json-v1"
CONTROLLED_EVIDENCE_SURFACE = "tool-and-actually-read-source-text-v1"
CONTROLLED_COUNTING_BOUNDARY = "tool-and-actually-read-source-text-v1"
CONTROLLED_OVERLAP_POLICY = "count-each-delivered-occurrence-v1"
USE_METRIC_COLUMNS = {
    "evidence_characters": "INTEGER",
    "estimated_evidence_tokens": "INTEGER",
    "evidence_metric_method": "TEXT",
    "evidence_metric_version": "TEXT",
    "evidence_metric_surface": "TEXT",
    "evidence_metric_quality": "TEXT",
}


def measure_text(text: str, *, surface: str) -> dict[str, Any]:
    if not isinstance(text, str) or not isinstance(surface, str) or not surface:
        raise IntelligenceError("Evidence measurement requires text and a named surface.")
    byte_count = len(text.encode("utf-8"))
    character_count = len(text)
    return {
        "evidence_bytes": byte_count,
        "evidence_characters": character_count,
        "estimated_evidence_tokens": (character_count + 3) // 4,
        "evidence_metric_method": CHAR4_ESTIMATOR_ID,
        "evidence_metric_version": CHAR4_ESTIMATOR_VERSION,
        "evidence_metric_surface": surface,
        "evidence_metric_quality": "MEASURED",
    }


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
                misleading_ever INTEGER NOT NULL DEFAULT 0,
                evidence_characters INTEGER, estimated_evidence_tokens INTEGER,
                evidence_metric_method TEXT, evidence_metric_version TEXT,
                evidence_metric_surface TEXT, evidence_metric_quality TEXT)"""
            )
            existing_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(uses)")
            }
            for name, column_type in USE_METRIC_COLUMNS.items():
                if name not in existing_columns:
                    try:
                        connection.execute(f"ALTER TABLE uses ADD COLUMN {name} {column_type}")
                    except sqlite3.OperationalError as exc:
                        if "duplicate column name" not in str(exc).lower():
                            raise
                        existing_columns = {
                            row["name"] for row in connection.execute("PRAGMA table_info(uses)")
                        }
                        if name not in existing_columns:
                            raise
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
        keys = (
            "id", "timestamp", "repository_key", "generation", "consumer", "operation",
            "latency_ms", "response_bytes", "nodes", "relationships", "outcome", "truncated",
            *USE_METRIC_COLUMNS,
        )
        values = tuple(row[key] for key in keys[:12]) + tuple(row.get(key) for key in keys[12:])
        db.execute(
            """INSERT INTO uses
            (id,timestamp,repository_key,generation,consumer,operation,latency_ms,response_bytes,
             nodes,relationships,outcome,truncated,evidence_characters,estimated_evidence_tokens,
             evidence_metric_method,evidence_metric_version,evidence_metric_surface,
             evidence_metric_quality) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            values,
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
    legacy_keys = {"id", "task", "control", "graphify", "assessment", "method"}
    controlled_keys = legacy_keys | {"provenance"}
    if not isinstance(payload, dict) or frozenset(payload) not in {frozenset(legacy_keys), frozenset(controlled_keys)}:
        raise IntelligenceError("Benchmark requires id/task/control/graphify/assessment/method.")
    for key, limit in (("id", 64), ("task", 240), ("assessment", 1200), ("method", 1200)):
        if not isinstance(payload[key], str) or not 0 < len(payload[key]) <= limit:
            raise IntelligenceError(f"Invalid benchmark {key}.")
    legacy_metrics = {"files", "source_bytes", "evidence_bytes", "interactions", "elapsed_ms"}
    controlled_metrics = legacy_metrics | {
        "files_inspected",
        "source_evidence_bytes", "tool_evidence_bytes", "total_evidence_bytes",
        "source_evidence_characters", "tool_evidence_characters", "total_evidence_characters",
        "estimated_evidence_tokens", "evidence_metric_method", "evidence_metric_version",
        "evidence_metric_surface", "evidence_metric_quality",
        "source_windows_read", "source_windows_located_only",
    }
    fixed_reference_metrics = controlled_metrics | {
        "reference_tokenizer_evidence_tokens", "reference_tokenizer_method",
        "reference_tokenizer_method_version", "reference_tokenizer_package_version",
        "reference_tokenizer_encoding", "reference_tokenizer_asset_sha256",
        "reference_tokenizer_surface", "reference_tokenizer_quality",
    }
    is_controlled = "provenance" in payload
    if is_controlled:
        _validate_benchmark_provenance(payload["provenance"])
    for arm in ("control", "graphify"):
        arm_keys = set(payload[arm]) if isinstance(payload[arm], dict) else set()
        if is_controlled and "reference_tokenizer_evidence_tokens" in arm_keys:
            expected_metrics = fixed_reference_metrics
        else:
            expected_metrics = controlled_metrics if is_controlled else legacy_metrics
        if not isinstance(payload[arm], dict) or set(payload[arm]) != expected_metrics:
            raise IntelligenceError("Benchmark arm metrics do not match the selected legacy or controlled schema.")
        for key, value in payload[arm].items():
            if value is None:
                if is_controlled and set(payload[arm]) == fixed_reference_metrics and key.startswith("reference_tokenizer_"):
                    continue
                raise IntelligenceError("Only fixed-reference measurement fields may be unknown.")
            if isinstance(value, str):
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise IntelligenceError("Benchmark metrics must be finite non-negative numbers.")
        if is_controlled:
            _validate_controlled_arm(payload[arm])
            if set(payload[arm]) == fixed_reference_metrics:
                _validate_reference_token_arm(payload[arm])
    if is_controlled:
        arm_schemas = [
            "reference_tokenizer_evidence_tokens" in payload[arm]
            for arm in ("control", "graphify")
        ]
        if arm_schemas[0] != arm_schemas[1]:
            raise IntelligenceError("Both controlled benchmark arms must use the same fixed-reference schema.")
    with database(root) as db:
        if db.execute("SELECT COUNT(*) FROM benchmarks").fetchone()[0] >= 100:
            raise IntelligenceError("Benchmark capacity reached.")
        db.execute("INSERT INTO benchmarks VALUES (?,?)", (payload["id"], json.dumps(payload)))


def _validate_benchmark_provenance(provenance: Any) -> None:
    base_required = {
        "recorded_at", "protocol_version", "corpus_id", "repository_id", "source_head",
        "source_identity", "question_id", "stopping_rule", "stopping_rules_matched",
        "counting_boundary", "overlap_policy",
    }
    fixed_required = base_required | {
        "generation", "control_strategy", "intelligence_strategy", "completion_criteria_matched",
    }
    if not isinstance(provenance, dict) or frozenset(provenance) not in {frozenset(base_required), frozenset(fixed_required)}:
        raise IntelligenceError("Controlled benchmark provenance is incomplete.")
    limits = [
        ("recorded_at", 48), ("protocol_version", 48), ("corpus_id", 120),
        ("repository_id", 120), ("source_head", 80), ("source_identity", 128),
        ("question_id", 120), ("stopping_rule", 500), ("counting_boundary", 120),
        ("overlap_policy", 80),
    ]
    if "generation" in provenance:
        limits.extend((
            ("generation", 80), ("control_strategy", 240),
            ("intelligence_strategy", 240),
        ))
    for key, limit in limits:
        value = provenance[key]
        if not isinstance(value, str) or not 0 < len(value) <= limit:
            raise IntelligenceError(f"Invalid controlled benchmark provenance {key}.")
    try:
        recorded_at = datetime.fromisoformat(provenance["recorded_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise IntelligenceError("Controlled benchmark recorded_at must be an ISO timestamp.") from exc
    if (
        provenance["protocol_version"] != "controlled-evidence-v1"
        or provenance["counting_boundary"] != CONTROLLED_COUNTING_BOUNDARY
        or provenance["overlap_policy"] != CONTROLLED_OVERLAP_POLICY
        or not isinstance(provenance["stopping_rules_matched"], bool)
        or recorded_at.tzinfo is None
        or "completion_criteria_matched" in provenance
        and not isinstance(provenance["completion_criteria_matched"], bool)
    ):
        raise IntelligenceError("Controlled benchmark protocol, counting boundary, overlap policy, or stop-rule status is invalid.")


def _validate_controlled_arm(arm: dict[str, Any]) -> None:
    integer_fields = {
        "files", "files_inspected", "source_bytes", "evidence_bytes", "interactions", "source_evidence_bytes",
        "tool_evidence_bytes", "total_evidence_bytes", "source_evidence_characters",
        "tool_evidence_characters", "total_evidence_characters", "estimated_evidence_tokens",
        "source_windows_read", "source_windows_located_only",
    }
    if any(not isinstance(arm[key], int) or isinstance(arm[key], bool) for key in integer_fields):
        raise IntelligenceError("Controlled benchmark counts must be non-negative integers.")
    for key, limit in (
        ("evidence_metric_method", 48), ("evidence_metric_version", 32),
        ("evidence_metric_surface", 120), ("evidence_metric_quality", 24),
    ):
        if not isinstance(arm[key], str) or not 0 < len(arm[key]) <= limit:
            raise IntelligenceError(f"Invalid controlled benchmark {key}.")
    if (
        arm["files"] != arm["files_inspected"]
        or arm["source_bytes"] != arm["source_evidence_bytes"]
        or arm["evidence_bytes"] != arm["total_evidence_bytes"]
        or arm["source_evidence_bytes"] + arm["tool_evidence_bytes"] != arm["total_evidence_bytes"]
        or arm["source_evidence_bytes"] < arm["source_evidence_characters"]
        or arm["tool_evidence_bytes"] < arm["tool_evidence_characters"]
        or arm["source_evidence_characters"] + arm["tool_evidence_characters"] != arm["total_evidence_characters"]
        or arm["estimated_evidence_tokens"] != (arm["total_evidence_characters"] + 3) // 4
        or arm["evidence_metric_method"] != CHAR4_ESTIMATOR_ID
        or arm["evidence_metric_version"] != CHAR4_ESTIMATOR_VERSION
        or arm["evidence_metric_surface"] != CONTROLLED_EVIDENCE_SURFACE
        or arm["evidence_metric_quality"] != "MEASURED"
    ):
        raise IntelligenceError("Controlled benchmark arm metrics are internally inconsistent.")


def _validate_reference_token_arm(arm: dict[str, Any]) -> None:
    token_count = arm["reference_tokenizer_evidence_tokens"]
    quality = arm["reference_tokenizer_quality"]
    metadata_fields = (
        "reference_tokenizer_method", "reference_tokenizer_method_version",
        "reference_tokenizer_package_version", "reference_tokenizer_encoding",
        "reference_tokenizer_asset_sha256", "reference_tokenizer_surface",
    )
    if quality == "NOT_MEASURED":
        if token_count is not None or any(arm[key] is not None for key in metadata_fields):
            raise IntelligenceError("Unavailable reference-tokenizer measurements must remain NULL.")
        return
    if (
        quality != "MEASURED"
        or isinstance(token_count, bool)
        or not isinstance(token_count, int)
        or any(
            not isinstance(arm[key], str) or not 0 < len(arm[key]) <= limit
            for key, limit in (
                ("reference_tokenizer_method", 64),
                ("reference_tokenizer_method_version", 32),
                ("reference_tokenizer_package_version", 32),
                ("reference_tokenizer_encoding", 64),
                ("reference_tokenizer_surface", 160),
            )
        )
        or not isinstance(arm["reference_tokenizer_asset_sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", arm["reference_tokenizer_asset_sha256"])
    ):
        raise IntelligenceError("Invalid fixed-reference tokenizer measurement provenance.")


def _percent_reduction(control: int, intelligence: int) -> float | None:
    if control == 0:
        return None
    return round((control - intelligence) / control * 100, 1)


def _benchmark_sort_key(row: dict[str, Any]) -> tuple[float, str]:
    provenance = row.get("provenance")
    recorded_at = provenance.get("recorded_at") if isinstance(provenance, dict) else None
    try:
        recorded = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
        if recorded.tzinfo is None:
            recorded = recorded.replace(tzinfo=timezone.utc)
        timestamp = recorded.timestamp()
    except (AttributeError, ValueError, OSError, OverflowError):
        timestamp = float("-inf")
    return timestamp, str(row.get("id", ""))


def _benchmark_display(payload: dict[str, Any]) -> dict[str, Any]:
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        return {
            **payload,
            "comparison": {
                "eligible": False, "method": "Legacy byte-only",
                "note": "Token counts were not measured; historical stopping-rule comparability is unknown.",
                "token_reduction_percent": None, "byte_reduction_percent": None,
            },
            "reference_comparison": {
                "eligible": False, "method": "Not measured",
                "token_reduction_percent": None,
                "note": "Fixed reference-tokenizer counts were not measured.",
            },
        }
    control = payload["control"]
    intelligence = payload["graphify"]
    eligible = (
        provenance.get("stopping_rules_matched") is True
        and provenance.get("completion_criteria_matched", True) is True
        and control.get("evidence_metric_method") == intelligence.get("evidence_metric_method")
        and control.get("evidence_metric_version") == intelligence.get("evidence_metric_version")
        and control.get("evidence_metric_surface") == intelligence.get("evidence_metric_surface")
        and control.get("evidence_metric_quality") == intelligence.get("evidence_metric_quality") == "MEASURED"
    )
    reference_fields = (
        "reference_tokenizer_method", "reference_tokenizer_method_version",
        "reference_tokenizer_package_version", "reference_tokenizer_encoding",
        "reference_tokenizer_asset_sha256", "reference_tokenizer_surface",
    )
    reference_eligible = (
        provenance.get("stopping_rules_matched") is True
        and provenance.get("completion_criteria_matched") is True
        and control.get("reference_tokenizer_quality") == intelligence.get("reference_tokenizer_quality") == "MEASURED"
        and control.get("reference_tokenizer_evidence_tokens") is not None
        and intelligence.get("reference_tokenizer_evidence_tokens") is not None
        and all(control.get(key) == intelligence.get(key) for key in reference_fields)
    )
    reference_method = (
        f'{control["reference_tokenizer_method"]} v{control["reference_tokenizer_method_version"]}'
        if control.get("reference_tokenizer_method")
        else "Not measured"
    )
    return {
        **payload,
        "comparison": {
            "eligible": eligible,
            "method": f'{control["evidence_metric_method"]} v{control["evidence_metric_version"]}',
            "note": (
                "Matched stopping rules and counting method."
                if eligible else "Not comparable: stopping rules or measurement provenance do not match."
            ),
            "token_reduction_percent": (
                _percent_reduction(control["estimated_evidence_tokens"], intelligence["estimated_evidence_tokens"])
                if eligible else None
            ),
            "byte_reduction_percent": (
                _percent_reduction(control["total_evidence_bytes"], intelligence["total_evidence_bytes"])
                if eligible else None
            ),
        },
        "reference_comparison": {
            "eligible": reference_eligible,
            "method": reference_method,
            "note": (
                "Matched fixed reference tokenizer, asset, evidence surface, and completion criteria."
                if reference_eligible
                else "Not comparable: fixed-tokenizer provenance or completion criteria do not match."
            ),
            "token_reduction_percent": (
                _percent_reduction(
                    control["reference_tokenizer_evidence_tokens"],
                    intelligence["reference_tokenizer_evidence_tokens"],
                )
                if reference_eligible else None
            ),
        },
    }


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
        benchmarks = [
            _benchmark_display(json.loads(row[0]))
            for row in db.execute("SELECT payload FROM benchmarks ORDER BY id")
        ]
        refreshes = [
            dict(row)
            for row in db.execute("SELECT * FROM refreshes ORDER BY timestamp DESC,id DESC LIMIT 30")
        ]
        refresh_attempts = db.execute("SELECT COUNT(*) FROM refreshes").fetchone()[0]
        completed_refreshes = db.execute(
            "SELECT COUNT(*) FROM refreshes WHERE completed=1"
        ).fetchone()[0]
    benchmarks.sort(key=_benchmark_sort_key, reverse=True)
    reference_benchmarks = [
        row for row in benchmarks if row["reference_comparison"]["eligible"]
    ]
    historical_benchmarks = [
        row for row in benchmarks if not row["reference_comparison"]["eligible"]
    ]
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
    token_groups: dict[tuple[str, str, str], list[int]] = {}
    for row in rows:
        if row["estimated_evidence_tokens"] is None or row["evidence_metric_quality"] != "MEASURED":
            continue
        key = (
            row["evidence_metric_method"], row["evidence_metric_version"],
            row["evidence_metric_surface"],
        )
        token_groups.setdefault(key, []).append(row["estimated_evidence_tokens"])
    token_aggregates = [
        {
            "method": method, "version": version, "surface": surface, "uses": len(values),
            "total": sum(values), "median": statistics.median(values),
        }
        for (method, version, surface), values in sorted(token_groups.items())
    ]
    benchmark_methods = sorted({row["comparison"]["method"] for row in benchmarks})
    reference_benchmark_methods = sorted({
        row["reference_comparison"]["method"]
        for row in benchmarks
        if row["reference_comparison"]["method"] != "Not measured"
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
        "reference_benchmarks": reference_benchmarks,
        "historical_benchmarks": historical_benchmarks,
        "latest_reference_benchmark": reference_benchmarks[0] if reference_benchmarks else None,
        "evidence_token_aggregates": token_aggregates,
        "benchmark_methods": benchmark_methods,
        "comparable_token_benchmarks": sum(row["comparison"]["eligible"] for row in benchmarks),
        "reference_benchmark_methods": reference_benchmark_methods,
        "comparable_reference_token_benchmarks": sum(
            row["reference_comparison"]["eligible"] for row in benchmarks
        ),
        "refresh_attempts": refresh_attempts,
        "completed_refreshes": completed_refreshes,
        "refreshes": refreshes,
    }
