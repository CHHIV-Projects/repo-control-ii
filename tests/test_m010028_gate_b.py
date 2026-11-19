"""Fixture-only tests for the M010.028 Gate B exact-plan boundary."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.test_m010028_gate_a import GateAFixture, OPS


GATE_B = OPS / "gate_b.py"
spec = importlib.util.spec_from_file_location("m010028_gate_b", GATE_B)
gate_b = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(gate_b)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def utc(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


class GateBFixture:
    def __init__(self, base: Path) -> None:
        a_base = base / "a"
        a_base.mkdir()
        self.a = GateAFixture(a_base)
        self.a.write_plan()
        prepared = self.a.candidate / "prepared"
        database = prepared / "managed-prepared.sqlite3"
        prepared.chmod(0o750)
        database.chmod(0o440)
        Path(f"{database}-wal").chmod(0o440)
        Path(f"{database}-shm").chmod(0o660)
        targets = []
        for path, held, released, kind in (
            (prepared, 0o750, 0o770, "directory"),
            (database, 0o440, 0o660, "file"),
            (Path(f"{database}-wal"), 0o440, 0o660, "file"),
            (Path(f"{database}-shm"), 0o660, 0o660, "file"),
        ):
            info = os.lstat(path)
            targets.append({
                "path": str(path), "device": info.st_dev, "inode": info.st_ino,
                "uid": info.st_uid, "gid": info.st_gid, "held_mode": held,
                "released_mode": released, "kind": kind,
            })
        self.nonce = "7" * 32
        self.plan = {
            "schema": gate_b.PLAN_SCHEMA,
            "gate_a_plan_path": str(self.a.plan_path),
            "gate_a_plan_sha256": sha(self.a.plan_path),
            "window_start_utc": utc(-5),
            "window_end_utc": utc(55),
            "helper_sha256": sha(GATE_B),
            "runner_sha256": sha(OPS / "gate-b-canary.sh"),
            "release_targets": targets,
            "canary": {
                "nonce": self.nonce,
                "summary": f"M010.028 Gate B one-write canary nonce={self.nonce}",
                "source": "Gate B exact-plan canary through repoctl coder-test",
                "result": "PASS: one minimally consequential managed workflow event; no Git action",
            },
            "evidence_path": str(base / "canary-evidence.json"),
        }
        self.plan_path = base / "gate-b-plan.json"
        self.plan_path.write_text(json.dumps(self.plan, indent=2, sort_keys=True) + "\n")

    def approve(self) -> tuple[Path, str]:
        plan, digest = gate_b.load_plan(self.plan_path)
        text = self.plan_path.parent / "approval.txt"
        text.write_text(gate_b.approval_phrase(digest, plan) + "\n")
        out = self.plan_path.parent / "approvals"
        out.mkdir()
        return gate_b.record_approval(str(self.plan_path), str(text), str(out))

    def close(self) -> None:
        self.a.health.stop()


class GateBTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.fx = GateBFixture(Path(self.temp.name))

    def tearDown(self) -> None:
        self.fx.close()
        self.temp.cleanup()

    def test_review_is_data_only_and_binds_exact_release_and_canary(self) -> None:
        plan, digest = gate_b.load_plan(self.fx.plan_path)
        with mock.patch.object(gate_b.subprocess, "run", side_effect=AssertionError("review executed a program")):
            review = gate_b.render_review(plan, digest)
        self.assertIn(gate_b.approval_phrase(digest, plan), review)
        self.assertIn("crosses the first-managed-write boundary", review)
        self.assertIn(self.fx.nonce, review)

    def test_approval_rejects_other_plan_or_canary(self) -> None:
        record, record_sha = self.fx.approve()
        gate_b.verify_approval(str(self.fx.plan_path), str(record), record_sha)
        self.fx.plan["canary"]["summary"] += " changed"
        self.fx.plan_path.write_text(json.dumps(self.fx.plan))
        with self.assertRaisesRegex(gate_b.GateBError, "approval"):
            gate_b.verify_approval(str(self.fx.plan_path), str(record), record_sha)

    def test_release_is_exact_inode_bound_and_root_only(self) -> None:
        plan, _ = gate_b.load_plan(self.fx.plan_path)
        with mock.patch.object(gate_b.os, "geteuid", return_value=1000):
            with self.assertRaisesRegex(gate_b.GateBError, "root"):
                gate_b.release(plan)
        with mock.patch.object(gate_b.os, "geteuid", return_value=0):
            gate_b.release(plan)
        gate_b.check_targets(plan, released=True)
        self.assertEqual(stat_mode(Path(plan["release_targets"][0]["path"])), 0o770)

    def test_release_refuses_replaced_target_before_any_chmod(self) -> None:
        plan, _ = gate_b.load_plan(self.fx.plan_path)
        target = Path(plan["release_targets"][1]["path"])
        replacement = target.with_suffix(".new")
        replacement.write_bytes(target.read_bytes())
        replacement.chmod(0o440)
        os.replace(replacement, target)
        with mock.patch.object(gate_b.os, "geteuid", return_value=0), \
             mock.patch.object(gate_b.os, "chmod", wraps=os.chmod) as chmod:
            with self.assertRaisesRegex(gate_b.GateBError, "exact held metadata"):
                gate_b.release(plan)
        chmod.assert_not_called()

    def test_canary_runs_once_and_reads_back_exact_event(self) -> None:
        plan, _ = gate_b.load_plan(self.fx.plan_path)
        with mock.patch.object(gate_b.os, "geteuid", return_value=0):
            gate_b.release(plan)
        event_id = "8" * 32
        event = {
            "event_id": event_id, "event_type": "test_result_recorded",
            "summary": plan["canary"]["summary"], "evidence_source": plan["canary"]["source"],
            "result": plan["canary"]["result"], "timestamp": utc(0),
        }
        calls: list[list[str]] = []

        def run(command, **kwargs):
            calls.append(command)
            if "coder-test" in command:
                return subprocess.CompletedProcess(command, 0, stdout=f"event_id:     {event_id}\n", stderr="")
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps({"workflow": {"recent_events": [event]}}), stderr=""
            )

        with mock.patch.object(gate_b.gate_a, "check_source_parity"), \
             mock.patch.object(gate_b.gate_a, "check_publisher_binding"), \
             mock.patch.object(gate_b.gate_a, "check_recovery_point"), \
             mock.patch.object(gate_b.gate_a, "readiness"), \
             mock.patch.object(gate_b.gate_a, "_backup_environment", return_value={
                 "REPOCTL_BACKUP_COVERAGE_PATH": "/tmp/coverage"
             }), mock.patch.object(gate_b.subprocess, "run", side_effect=run):
            self.assertEqual(gate_b.run_canary(plan, str(self.fx.plan_path)), event_id)
        self.assertEqual(sum("coder-test" in command for command in calls), 1)
        self.assertTrue(Path(plan["evidence_path"]).is_file())

    def test_ambiguous_canary_never_retries(self) -> None:
        plan, _ = gate_b.load_plan(self.fx.plan_path)
        with mock.patch.object(gate_b.os, "geteuid", return_value=0):
            gate_b.release(plan)
        calls = 0

        def fail(command, **kwargs):
            nonlocal calls
            calls += 1
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="lost response")

        with mock.patch.object(gate_b.gate_a, "check_source_parity"), \
             mock.patch.object(gate_b.gate_a, "check_publisher_binding"), \
             mock.patch.object(gate_b.gate_a, "check_recovery_point"), \
             mock.patch.object(gate_b.gate_a, "readiness"), \
             mock.patch.object(gate_b.gate_a, "_backup_environment", return_value={
                 "REPOCTL_BACKUP_COVERAGE_PATH": "/tmp/coverage"
             }), mock.patch.object(gate_b.subprocess, "run", side_effect=fail):
            with self.assertRaises(gate_b.CommitUncertain):
                gate_b.run_canary(plan, str(self.fx.plan_path))
        self.assertEqual(calls, 1)

    def test_post_canary_backup_must_be_bound_and_newer_than_event(self) -> None:
        plan, digest = gate_b.load_plan(self.fx.plan_path)
        event_time = datetime.now(timezone.utc)
        event = {"event_id": "8" * 32, "timestamp": event_time.isoformat()}
        Path(plan["evidence_path"]).write_text(json.dumps({
            "schema": "repoctl.m010028.gate-b-canary-evidence.v1",
            "gate_b_plan_sha256": digest,
            "event_id": event["event_id"],
            "event": event,
        }))
        with mock.patch.object(gate_b.gate_a, "check_publisher_binding") as publisher, \
             mock.patch.object(gate_b.gate_a, "check_recovery_point", return_value={
                 "verified_at_utc": (event_time + timedelta(seconds=1)).isoformat()
             }):
            record = gate_b.post_canary_backup(plan, str(self.fx.plan_path))
        publisher.assert_called_once()
        self.assertIn("verified_at_utc", record)
        with mock.patch.object(gate_b.gate_a, "check_publisher_binding"), \
             mock.patch.object(gate_b.gate_a, "check_recovery_point", return_value={
                 "verified_at_utc": (event_time - timedelta(seconds=1)).isoformat()
             }):
            with self.assertRaisesRegex(gate_b.GateBError, "predates"):
                gate_b.post_canary_backup(plan, str(self.fx.plan_path))

    def test_separate_restore_must_contain_exact_canary_payload(self) -> None:
        plan, digest = gate_b.load_plan(self.fx.plan_path)
        event = {
            "event_id": "8" * 32,
            "event_type": "test_result_recorded",
            "summary": plan["canary"]["summary"],
            "evidence_source": plan["canary"]["source"],
            "result": plan["canary"]["result"],
            "timestamp": utc(0),
        }
        Path(plan["evidence_path"]).write_text(json.dumps({
            "schema": "repoctl.m010028.gate-b-canary-evidence.v1",
            "gate_b_plan_sha256": digest,
            "event_id": event["event_id"],
            "event": event,
        }))
        restore_root = self.fx.plan_path.parent / "restore"
        restore_root.mkdir()

        def restore(command, **kwargs):
            restored = restore_root / f"gate-b-{event['event_id']}"
            with sqlite3.connect(restored) as connection:
                connection.execute(
                    "CREATE TABLE workflow_event (repository_id TEXT, milestone_id TEXT, "
                    "event_id TEXT, event_type TEXT, payload_json TEXT)"
                )
                connection.execute(
                    "INSERT INTO workflow_event VALUES (?, ?, ?, ?, ?)",
                    (self.fx.a.binding["repository_id"], "M010.028", event["event_id"],
                     event["event_type"], json.dumps(event)),
                )
            return subprocess.CompletedProcess(command, 0, stdout="restored", stderr="")

        with mock.patch.object(gate_b, "post_canary_backup", return_value={
            "manifest_path": "/nas/exact.manifest.json"
        }), mock.patch.object(gate_b.gate_a, "_backup_environment", return_value={
            "REPOCTL_BACKUP_RESTORE_ROOT": str(restore_root)
        }), mock.patch.object(gate_b.subprocess, "run", side_effect=restore):
            restored = gate_b.restore_canary_backup(plan, str(self.fx.plan_path))
        self.assertTrue(restored.is_file())


class GateBRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls.log"
        python = self.bin / "python"
        python.write_text("""#!/usr/bin/env bash
echo "python $2" >> "$GATEB_LOG"
case "$2" in
  run-canary) exit "${GATEB_CANARY_RC:-0}" ;;
  backup-service) echo repo-control-backup-r9.service ;;
esac
exit 0
""")
        sudo = self.bin / "sudo"
        sudo.write_text("""#!/usr/bin/env bash
echo "sudo $*" >> "$GATEB_LOG"
[[ "$1" == "-v" ]] && exit 0
[[ "$1" == "-n" ]] && shift
exec "$@"
""")
        systemctl = self.bin / "systemctl"
        systemctl.write_text("""#!/usr/bin/env bash
echo "systemctl $*" >> "$GATEB_LOG"
exit 0
""")
        for path in (python, sudo, systemctl):
            path.chmod(0o755)
        self.runner = self.root / "gate-b-canary.sh"
        text = (OPS / "gate-b-canary.sh").read_text().replace(
            "PY=/home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python",
            f"PY={python}",
        )
        self.runner.write_text(text)
        self.runner.chmod(0o755)
        shutil.copy(OPS / "gate_b.py", self.root / "gate_b.py")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_runner(self, canary_rc: int = 0) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "GATEB_LOG": str(self.log),
            "GATEB_CANARY_RC": str(canary_rc),
        }
        return subprocess.run(
            ["bash", str(self.runner), "--plan", "/p", "--approval-record", "/a",
             "--approval-sha256", "0" * 64],
            env=env, capture_output=True, text=True, timeout=10,
        )

    def test_runner_invokes_one_canary_and_blocking_backup_start(self) -> None:
        result = self.run_runner()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.log.read_text().splitlines()
        self.assertEqual(calls.count("python run-canary"), 1)
        self.assertIn("systemctl start repo-control-backup-r9.service", calls)
        self.assertNotIn("ExecMainStartTimestampMonotonic", "\n".join(calls))
        self.assertIn("python post-canary-backup", calls)
        self.assertIn("python restore-canary-backup", calls)

    def test_runner_never_backs_up_or_retries_after_ambiguous_canary(self) -> None:
        result = self.run_runner(canary_rc=4)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        calls = self.log.read_text().splitlines()
        self.assertEqual(calls.count("python run-canary"), 1)
        self.assertFalse(any(call.startswith("systemctl ") for call in calls))
        self.assertNotIn("python post-canary-backup", calls)
        self.assertNotIn("python restore-canary-backup", calls)


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


if __name__ == "__main__":
    unittest.main()
