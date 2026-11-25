"""M011.004 test-only policy prototype; never imported by the live monitor."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import statistics
import tempfile
import time
import tracemalloc
from typing import Any, Literal, Mapping, TypedDict
from unittest.mock import patch

from repoctl import backup, backup_guard
from repoctl.information_store import StoreIdentity


class ProbeResult(TypedDict):
    ready: bool
    code: str
    message: str
    freshness_code: str
    history_code: str
    history_checked_at: str | None
    manifest_path: str | None


@dataclass(frozen=True)
class ScrubResult:
    checked_at: datetime
    checked_count: int
    invalid_manifests: tuple[str, ...]


def fixture_binding(root: Path, identity: StoreIdentity) -> dict[str, Any]:
    return {
        "authority": {
            "configuration_sha256": "a" * 64,
            "database_path": str(root / "source.sqlite3"),
            "generation": "b" * 32,
            "registry_path": str(root / "fixture-registry.toml"),
            "registry_sha256": "c" * 64,
            "state_root": str(root / "fixture-state"),
        },
        "database_identity": "d" * 64,
        "repository": {
            "repository_id": identity.repository_id,
            "repository_key": identity.repository_key,
            "repository_root": identity.repository_root,
        },
        "selection": {
            "milestone_id": identity.milestone_id,
            "prompt_path": identity.prompt_path,
            "selection_id": identity.selection_id,
            "selection_revision": identity.selection_revision,
        },
    }


def full_scrub(destination: Path, identity: StoreIdentity, *, now: datetime) -> ScrubResult:
    directory = backup._require_directory(destination, "Fixture catalog", writable=False)
    invalid: list[str] = []
    count = 0
    for path in directory.glob("*.manifest.json"):
        count += 1
        if backup._read_manifest_for_monitor(
            path,
            expected_repository_id=identity.repository_id,
            expected_repository_key=identity.repository_key,
            expected_repository_root=identity.repository_root,
        ) is None:
            invalid.append(path.name)
    return ScrubResult(now, count, tuple(sorted(invalid)))


def probe_latest(
    destination: Path,
    record_path: Path,
    lock_path: Path,
    identity: StoreIdentity,
    binding: Mapping[str, Any],
    *,
    now: datetime,
    history: ScrubResult | None,
    last_job_state: Literal["success", "failed", "unknown"],
    publication_complete: bool,
) -> ProbeResult:
    # Job completion/durability and the scrub receipt are fixture inputs, not
    # existing production evidence providers. Their integration needs approval.
    history_code = "DUE"
    observed_manifest: str | None = None
    if history is not None:
        if history.invalid_manifests:
            history_code = "FAILED"
        elif history.checked_count and timedelta(0) <= now - history.checked_at < timedelta(minutes=70):
            history_code = "VERIFIED_AS_OF"

    def result(code: str, message: str, freshness: str = "NOT_VERIFIED") -> ProbeResult:
        return {
            "ready": code == "READY",
            "code": code,
            "message": message,
            "freshness_code": freshness,
            "history_code": history_code,
            "history_checked_at": history.checked_at.isoformat() if history is not None else None,
            "manifest_path": observed_manifest,
        }

    if last_job_state not in ("success", "failed", "unknown"):
        raise ValueError("Unsupported fixture backup-job state.")
    if last_job_state != "success":
        return result("BACKUP_JOB_UNVERIFIED", f"Latest backup job is {last_job_state}; no older fallback.")
    if not publication_complete:
        return result("PUBLICATION_PENDING", "Final durable publication is not established.")
    try:
        descriptor = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        return result("LOCK_UNAVAILABLE", f"Backup publication lock is unavailable: {exc}")
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return result("LOCK_UNAVAILABLE", "Backup publication lock is not a regular file.")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return result("BACKUP_IN_PROGRESS", "Backup still holds its publication lock; retry later.")
        except OSError as exc:
            return result("LOCK_UNAVAILABLE", f"Backup publication lock failed: {exc}")
        try:
            record = backup_guard.read_recovery_point(
                record_path, binding, now=now, max_age_seconds=1200,
            )
        except backup_guard.BackupEvidenceError as exc:
            return result("RECOVERY_UNVERIFIED", str(exc))
        try:
            path = Path(record["manifest_path"])
            observed_manifest = str(path)
            directory = backup._require_directory(destination, "Fixture catalog", writable=False)
            if (
                path.resolve(strict=True) != path
                or path.parent != directory
                or path.name.startswith(".")
                or not path.name.endswith(".manifest.json")
            ):
                return result("NOT_PUBLISHED", "The exact record does not name a final canonical manifest.")
            verified = backup._read_manifest_for_monitor(
                path,
                expected_repository_id=identity.repository_id,
                expected_repository_key=identity.repository_key,
                expected_repository_root=identity.repository_root,
            )
            if verified is None:
                return result("PAYLOAD_UNVERIFIED", "Exact manifest or payload failed verification; no fallback.")
            manifest = json.loads(backup._read_manifest_bytes(path))
            if not isinstance(manifest, dict) or any(
                manifest.get(key) != record[key]
                for key in ("manifest_sha256", "database_sha256", "snapshot_sha256", "verified_at_utc")
            ):
                return result("RECORD_MISMATCH", "Exact recovery record and manifest no longer agree.")
        except (OSError, backup.BackupError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return result("EVIDENCE_UNAVAILABLE", f"Exact recovery evidence is unavailable: {exc}")
        if history_code != "VERIFIED_AS_OF":
            return result(
                f"HISTORY_{history_code}",
                f"Latest payload verified; historical integrity is {history_code.lower()}.",
                "VERIFIED",
            )
        return result("READY", "Latest exact payload verified; historical integrity verified only as of receipt.", "VERIFIED")
    finally:
        os.close(descriptor)


def benchmark() -> dict[str, Any]:
    def memory() -> dict[str, int]:
        return {
            key: int(value.split()[0])
            for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines()
            for key, value in (line.split(":", 1),)
            if key in ("VmRSS", "VmHWM")
        }

    with tempfile.TemporaryDirectory(prefix="m011004-monitor-", dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        destination = root / "catalog"
        destination.mkdir(mode=0o700)
        lock = root / ".repoctl-backup.lock"
        lock.touch(mode=0o600)
        identity = StoreIdentity(
            repository_id="fixture-id", repository_key="fixture-key", repository_root=str(root),
            milestone_id="M999.001", prompt_path="prompt.md", selection_id="selection", selection_revision=1,
        )
        binding = fixture_binding(root, identity)
        now = datetime(2026, 11, 24, 12, tzinfo=UTC)
        snapshot = {"repository": binding["repository"]}
        count, size = 493, 128 * 1024
        for index in range(count):
            payload = index.to_bytes(4, "big") + bytes(size - 4)
            database = destination / f"fixture-{index:04}.sqlite3"
            database.write_bytes(payload)
            manifest: dict[str, Any] = {
                "manifest_version": backup.MANIFEST_VERSION,
                "verified": True,
                "verified_at_utc": (now - timedelta(seconds=count - index)).isoformat(),
                "database_file": database.name,
                "database_bytes": size,
                "database_sha256": hashlib.sha256(payload).hexdigest(),
                "snapshot": snapshot,
                "snapshot_sha256": hashlib.sha256(backup._canonical_json(snapshot)).hexdigest(),
                "integrity_check": "ok",
                "foreign_key_violations": 0,
            }
            manifest["manifest_sha256"] = hashlib.sha256(backup._canonical_json(manifest)).hexdigest()
            manifest_path = destination / f"fixture-{index:04}.manifest.json"
            manifest_path.write_bytes(backup._canonical_json(manifest))
        record_path = root / "recovery.json"
        backup_guard.write_recovery_point(record_path, manifest_path=manifest_path, manifest=manifest, binding=binding)
        history = full_scrub(destination, identity, now=now)
        original_hash = backup._sha256_file
        measurements: dict[str, Any] = {}
        for mode, cycles in (("current_full_catalog", 3), ("proposed_exact_point", 12)):
            reads: list[int] = []
            elapsed: list[float] = []
            total_bytes = 0

            def measured_hash(path: Path) -> str:
                nonlocal total_bytes
                total_bytes += path.stat().st_size
                reads.append(1)
                return original_hash(path)

            before_fds = len(list(Path("/proc/self/fd").iterdir()))
            before_memory = memory()
            cpu_start = time.process_time()
            tracemalloc.start()
            with patch.object(backup, "_sha256_file", side_effect=measured_hash):
                for _ in range(cycles):
                    started = time.perf_counter()
                    if mode == "current_full_catalog":
                        status = backup.check_backup_freshness(
                            destination,
                            expected_repository_id=identity.repository_id,
                            expected_repository_key=identity.repository_key,
                            expected_repository_root=identity.repository_root,
                            now=now,
                        )
                        if not status.healthy:
                            raise AssertionError(status.message)
                    else:
                        report = probe_latest(
                            destination, record_path, lock, identity, binding, now=now,
                            history=history, last_job_state="success", publication_complete=True,
                        )
                        if not report["ready"]:
                            raise AssertionError(report)
                    elapsed.append(time.perf_counter() - started)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            after_memory = memory()
            measurements[mode] = {
                "cycles": cycles,
                "hash_calls": len(reads),
                "payload_bytes_hashed": total_bytes,
                "median_seconds": statistics.median(elapsed),
                "max_seconds": max(elapsed),
                "cpu_seconds": time.process_time() - cpu_start,
                "python_peak_bytes": peak,
                "process_peak_rss_kib": after_memory["VmHWM"],
                "process_rss_kib": after_memory["VmRSS"],
                "rss_delta_kib": after_memory["VmRSS"] - before_memory["VmRSS"],
                "fd_delta": len(list(Path("/proc/self/fd").iterdir())) - before_fds,
            }
            expected_reads = cycles * (count if mode == "current_full_catalog" else 1)
            if len(reads) != expected_reads or total_bytes != expected_reads * size:
                raise AssertionError("Measured hash work differs from the prototype's stated scope.")
            if mode == "proposed_exact_point" and (
                max(elapsed) >= 30 or after_memory["VmHWM"] >= 128 * 1024
                or measurements[mode]["fd_delta"] != 0
            ):
                raise AssertionError("The local prototype exceeded its time, RSS or descriptor budget.")
        return {
            "environment": "local warm-cache synthetic hash corpus; not SQLite restore or NAS proof",
            "catalog_manifests": count,
            "catalog_payload_bytes": count * size,
            "measurements": measurements,
            "historical_policy": "prototype only: hourly scrub, 70-minute receipt expiry; not approved",
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", action="store_true", required=True)
    parser.parse_args()
    print(json.dumps(benchmark(), indent=2, sort_keys=True))
