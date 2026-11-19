"""M010.028 Gate B exact-plan hold release and one-canary controls."""

from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate_a


PLAN_SCHEMA = "repoctl.m010028.gate-b-plan.v1"
APPROVAL_SCHEMA = "repoctl.m010028.gate-b-approval.v1"
TARGET_KEYS = {"path", "device", "inode", "uid", "gid", "held_mode", "released_mode", "kind"}
CANARY_KEYS = {"nonce", "summary", "source", "result"}
TOP_LEVEL_KEYS = {
    "schema", "gate_a_plan_path", "gate_a_plan_sha256", "window_start_utc", "window_end_utc",
    "helper_sha256", "runner_sha256", "release_targets", "canary", "evidence_path",
}
RUNNER = Path(__file__).with_name("gate-b-canary.sh")


class GateBError(Exception):
    pass


class CommitUncertain(GateBError):
    pass


def sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _parse_utc(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise GateBError(f"Invalid UTC timestamp: {value}") from exc


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_absolute(path: Any, label: str) -> str:
    if not isinstance(path, str) or not path.startswith("/") or ".." in Path(path).parts:
        raise GateBError(f"{label} must be an absolute path without '..'.")
    return path


def load_plan(path: str | Path) -> tuple[dict[str, Any], str]:
    raw = Path(path).read_bytes()
    try:
        plan = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateBError(f"The Gate B plan is not valid JSON: {exc}") from exc
    if not isinstance(plan, dict) or set(plan) != TOP_LEVEL_KEYS or plan.get("schema") != PLAN_SCHEMA:
        raise GateBError("The Gate B plan fields or schema differ from the reviewed contract.")
    for key in ("gate_a_plan_path", "evidence_path"):
        _validate_absolute(plan[key], key)
    for key in ("gate_a_plan_sha256", "helper_sha256", "runner_sha256"):
        if not isinstance(plan[key], str) or re.fullmatch(gate_a._SHA, plan[key]) is None:
            raise GateBError(f"{key} must be a SHA-256 digest.")
    start, end = _parse_utc(plan["window_start_utc"]), _parse_utc(plan["window_end_utc"])
    if not 0 < (end - start).total_seconds() <= gate_a.MAX_WINDOW_SECONDS:
        raise GateBError("The Gate B window must be positive and at most four hours.")
    canary = plan["canary"]
    if not isinstance(canary, dict) or set(canary) != CANARY_KEYS:
        raise GateBError("The Gate B canary fields differ from the reviewed contract.")
    if re.fullmatch(r"[0-9a-f]{32}", canary.get("nonce", "")) is None:
        raise GateBError("The canary nonce must be 32 lowercase hexadecimal characters.")
    if any(not isinstance(canary[key], str) or not canary[key].strip() for key in ("summary", "source", "result")):
        raise GateBError("The canary payload fields must be non-empty strings.")
    if canary["nonce"] not in canary["summary"]:
        raise GateBError("The exact canary summary must contain its unique nonce.")
    targets = plan["release_targets"]
    if not isinstance(targets, list) or len(targets) != 4:
        raise GateBError("The Gate B plan must contain exactly four release targets.")
    paths: set[str] = set()
    for target in targets:
        if not isinstance(target, dict) or set(target) != TARGET_KEYS:
            raise GateBError("A Gate B release target differs from the reviewed contract.")
        target_path = _validate_absolute(target["path"], "release target")
        if target_path in paths:
            raise GateBError("The Gate B plan repeats a release target.")
        paths.add(target_path)
        if target["kind"] not in ("directory", "file"):
            raise GateBError("A Gate B release target has an invalid kind.")
        for key in ("device", "inode", "uid", "gid", "held_mode", "released_mode"):
            if type(target[key]) is not int or target[key] < 0:
                raise GateBError(f"Release target {key} must be a non-negative integer.")
    gate_a_plan, _ = gate_a.load_plan(plan["gate_a_plan_path"])
    database, wal, shm = gate_a._held_database(gate_a_plan)
    expected_targets = {
        str(Path(database).parent): ("directory", 0o750, 0o770),
        database: ("file", 0o440, 0o660),
        wal: ("file", 0o440, 0o660),
        shm: ("file", 0o660, 0o660),
    }
    actual_targets = {
        target["path"]: (target["kind"], target["held_mode"], target["released_mode"])
        for target in targets
    }
    if actual_targets != expected_targets:
        raise GateBError("The release targets do not exactly match the Gate A database contract.")
    return plan, hashlib.sha256(raw).hexdigest()


def approval_phrase(plan_sha256: str, plan: dict[str, Any]) -> str:
    return (
        f"GATE B APPROVED plan_sha256={plan_sha256} "
        f"window={plan['window_start_utc']}/{plan['window_end_utc']} "
        f"canary_nonce={plan['canary']['nonce']}"
    )


def check_window(plan: dict[str, Any]) -> None:
    if not _parse_utc(plan["window_start_utc"]) <= _now() < _parse_utc(plan["window_end_utc"]):
        raise GateBError("The current time is outside the Gate B plan window.")


def _target(path: Path, held_mode: int, released_mode: int, kind: str) -> dict[str, Any]:
    info = os.lstat(path)
    expected = stat.S_ISDIR(info.st_mode) if kind == "directory" else stat.S_ISREG(info.st_mode)
    if stat.S_ISLNK(info.st_mode) or not expected or stat.S_IMODE(info.st_mode) != held_mode:
        raise GateBError(f"Cannot bind release target with unexpected type or mode: {path}")
    return {
        "path": str(path), "device": info.st_dev, "inode": info.st_ino,
        "uid": info.st_uid, "gid": info.st_gid, "held_mode": held_mode,
        "released_mode": released_mode, "kind": kind,
    }


def _check_evidence_parent(path: str) -> None:
    parent = Path(path).parent
    info = os.lstat(parent)
    if (
        stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise GateBError("The canary evidence parent must be a real user-owned mode-0700 directory.")


def build_plan(
    gate_a_plan_path: str, window_start: str, window_end: str, nonce: str, evidence_path: str
) -> dict[str, Any]:
    gate_a_plan, gate_a_sha = gate_a.load_plan(gate_a_plan_path)
    gate_a.readiness(gate_a_plan, "managed")
    if gate_a.check_held(gate_a_plan) not in ("UNCHANGED held hashes and inventory",):
        raise GateBError("Gate B plan creation requires an unchanged held candidate.")
    gate_a.check_source_parity(gate_a_plan)
    gate_a.check_publisher_binding(gate_a_plan)
    gate_a.check_recovery_point(gate_a_plan)
    database, wal, shm = gate_a._held_database(gate_a_plan)
    prepared = Path(database).parent
    summary = f"M010.028 Gate B one-write canary nonce={nonce}"
    _check_evidence_parent(evidence_path)
    targets = [
        _target(prepared, 0o750, 0o770, "directory"),
        _target(Path(database), 0o440, 0o660, "file"),
        _target(Path(wal), 0o440, 0o660, "file"),
        _target(Path(shm), 0o660, 0o660, "file"),
    ]
    service = gate_a._unit_directives(gate_a_plan["managed_unit_source"]).get("Service", {})
    try:
        service_gid = grp.getgrnam(gate_a._one(service, "Group")).gr_gid
    except KeyError as exc:
        raise GateBError(f"The managed service group does not exist: {exc}") from exc
    if any(target["uid"] != 0 or target["gid"] != service_gid for target in targets):
        raise GateBError("Every Gate B release target must be root-owned and group-bound to the service.")
    plan = {
        "schema": PLAN_SCHEMA,
        "gate_a_plan_path": str(Path(gate_a_plan_path).resolve()),
        "gate_a_plan_sha256": gate_a_sha,
        "window_start_utc": window_start,
        "window_end_utc": window_end,
        "helper_sha256": sha256_file(__file__),
        "runner_sha256": sha256_file(RUNNER),
        "release_targets": targets,
        "canary": {
            "nonce": nonce,
            "summary": summary,
            "source": "Gate B exact-plan canary through repoctl coder-test",
            "result": "PASS: one minimally consequential managed workflow event; no Git action",
        },
        "evidence_path": _validate_absolute(evidence_path, "evidence_path"),
    }
    # Validate the window and complete shape before the caller persists it.
    start, end = _parse_utc(window_start), _parse_utc(window_end)
    if not 0 < (end - start).total_seconds() <= gate_a.MAX_WINDOW_SECONDS:
        raise GateBError("The Gate B window must be positive and at most four hours.")
    return plan


def render_review(plan: dict[str, Any], plan_sha256: str) -> str:
    lines = [
        "M010.028 GATE B REVIEW (data only; this review cannot release or write)",
        "",
        f"plan_sha256       {plan_sha256}",
        f"Gate A plan       {plan['gate_a_plan_sha256']} ({plan['gate_a_plan_path']})",
        f"Gate B helper     {plan['helper_sha256']}",
        f"Gate B runner     {plan['runner_sha256']}",
        f"window            {plan['window_start_utc']}/{plan['window_end_utc']}",
        f"canary summary     {plan['canary']['summary']}",
        f"canary source      {plan['canary']['source']}",
        f"canary result      {plan['canary']['result']}",
        "release targets:",
    ]
    for target in plan["release_targets"]:
        lines.append(
            f"  {target['path']} dev:inode={target['device']}:{target['inode']} "
            f"uid:gid={target['uid']}:{target['gid']} "
            f"mode={target['held_mode']:04o}->{target['released_mode']:04o}"
        )
    lines.extend([
        "",
        "This approval crosses the first-managed-write boundary. Exactly one canary is allowed.",
        "After the canary, the exact publisher runs once and a separate restore must contain that event.",
        "A failure after invocation is COMMIT UNCERTAIN and must be resolved read-only; never retry.",
        "Exact Product Owner phrase:",
        f"  {approval_phrase(plan_sha256, plan)}",
    ])
    return "\n".join(lines) + "\n"


def record_approval(plan_path: str, text_file: str, out_dir: str) -> tuple[Path, str]:
    plan, plan_sha = load_plan(plan_path)
    text = Path(text_file).read_text(encoding="utf-8")
    if approval_phrase(plan_sha, plan) not in text:
        raise GateBError("The approval text does not contain the exact Gate B phrase.")
    check_window(plan)
    record = {
        "schema": APPROVAL_SCHEMA,
        "plan_sha256": plan_sha,
        "approval_text": text,
        "approval_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "recorded_at_utc": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    payload = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()
    target = Path(out_dir) / f"gate-b-approval-{plan_sha[:16]}.json"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
    return target, hashlib.sha256(payload).hexdigest()


def verify_approval(plan_path: str, record_path: str, record_sha256: str) -> dict[str, Any]:
    plan, plan_sha = load_plan(plan_path)
    if sha256_file(__file__) != plan["helper_sha256"]:
        raise GateBError("The Gate B helper differs from the reviewed plan.")
    if sha256_file(RUNNER) != plan["runner_sha256"]:
        raise GateBError("The Gate B runner differs from the reviewed plan.")
    if sha256_file(plan["gate_a_plan_path"]) != plan["gate_a_plan_sha256"]:
        raise GateBError("The Gate A plan differs from the Gate B plan binding.")
    raw = Path(record_path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != record_sha256:
        raise GateBError("The Gate B approval record SHA-256 differs.")
    record = json.loads(raw)
    if not isinstance(record, dict) or set(record) != {
        "schema", "plan_sha256", "approval_text", "approval_text_sha256", "recorded_at_utc"
    } or record.get("schema") != APPROVAL_SCHEMA:
        raise GateBError("The Gate B approval record schema differs.")
    text = record["approval_text"]
    if (
        record["plan_sha256"] != plan_sha
        or hashlib.sha256(text.encode()).hexdigest() != record["approval_text_sha256"]
        or approval_phrase(plan_sha, plan) not in text
    ):
        raise GateBError("The Gate B approval does not bind this exact plan and canary.")
    check_window(plan)
    return plan


def check_targets(plan: dict[str, Any], *, released: bool) -> None:
    for target in plan["release_targets"]:
        info = os.lstat(target["path"])
        expected_type = stat.S_ISDIR(info.st_mode) if target["kind"] == "directory" else stat.S_ISREG(info.st_mode)
        expected_mode = target["released_mode"] if released else target["held_mode"]
        if (
            stat.S_ISLNK(info.st_mode) or not expected_type
            or (info.st_dev, info.st_ino, info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
            != (target["device"], target["inode"], target["uid"], target["gid"], expected_mode)
        ):
            state = "released" if released else "held"
            raise GateBError(f"Release target does not match its exact {state} metadata: {target['path']}")


def preflight(plan: dict[str, Any]) -> None:
    gate_a_plan, _ = gate_a.load_plan(plan["gate_a_plan_path"])
    check_window(plan)
    check_targets(plan, released=False)
    if gate_a.check_held(gate_a_plan) != "UNCHANGED held hashes and inventory":
        raise GateBError("Gate B requires an unchanged held candidate before release.")
    gate_a.check_source_parity(gate_a_plan)
    gate_a.check_publisher_binding(gate_a_plan)
    gate_a.check_recovery_point(gate_a_plan)
    gate_a.readiness(gate_a_plan, "managed")
    check_window(plan)


def release(plan: dict[str, Any]) -> None:
    if os.geteuid() != 0:
        raise GateBError("The exact Gate B hold release must run as root.")
    check_targets(plan, released=False)
    # Make the containing directory writable last to minimize the release interval.
    for target in sorted(plan["release_targets"], key=lambda item: item["kind"] == "directory"):
        if target["released_mode"] != target["held_mode"]:
            os.chmod(target["path"], target["released_mode"], follow_symlinks=False)
    check_targets(plan, released=True)


def run_canary(plan: dict[str, Any], plan_path: str) -> str:
    check_targets(plan, released=True)
    gate_a_plan, _ = gate_a.load_plan(plan["gate_a_plan_path"])
    gate_a.check_source_parity(gate_a_plan)
    gate_a.check_publisher_binding(gate_a_plan)
    gate_a.check_recovery_point(gate_a_plan)
    gate_a.readiness(gate_a_plan, "managed")
    check_window(plan)
    env_values = gate_a._backup_environment(gate_a_plan)
    command = [
        gate_a.PINNED_PYTHON, "-m", "repoctl.cli", "coder-test",
        "--repository", gate_a_plan["expected_binding"]["repository_root"],
        "--state-root", gate_a_plan["expected_binding"]["authority_state_root"],
        "--source", plan["canary"]["source"], "--result", plan["canary"]["result"],
        plan["canary"]["summary"],
    ]
    environment = {**os.environ, "REPOCTL_BACKUP_COVERAGE_PATH": env_values["REPOCTL_BACKUP_COVERAGE_PATH"]}
    result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=60, check=False)
    match = re.search(r"^event_id:\s+([0-9a-f]{32})$", result.stdout, re.MULTILINE)
    if result.returncode != 0 or match is None:
        raise CommitUncertain(
            "The one canary was invoked but did not return an unambiguous event ID. "
            "Do not retry; inspect the managed workflow read-only."
        )
    event_id = match.group(1)
    status_command = [
        gate_a.PINNED_PYTHON, "-m", "repoctl.cli", "coder-status", "--format", "json",
        "--repository", gate_a_plan["expected_binding"]["repository_root"],
        "--state-root", gate_a_plan["expected_binding"]["authority_state_root"],
    ]
    status = subprocess.run(status_command, env=environment, capture_output=True, text=True, timeout=60, check=False)
    try:
        payload = json.loads(status.stdout)
    except json.JSONDecodeError as exc:
        raise CommitUncertain("Canary commit returned an ID but read-only status was unavailable; do not retry.") from exc
    matches = [
        event for event in payload.get("workflow", {}).get("recent_events", [])
        if event.get("event_id") == event_id
        and event.get("event_type") == "test_result_recorded"
        and event.get("summary") == plan["canary"]["summary"]
        and event.get("evidence_source") == plan["canary"]["source"]
        and event.get("result") == plan["canary"]["result"]
    ]
    if status.returncode != 0 or len(matches) != 1:
        raise CommitUncertain("Canary event identity or payload did not read back exactly; do not retry.")
    evidence = {
        "schema": "repoctl.m010028.gate-b-canary-evidence.v1",
        "gate_b_plan_sha256": hashlib.sha256(Path(plan_path).read_bytes()).hexdigest(),
        "event_id": event_id,
        "event": matches[0],
        "verified_at_utc": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    target = Path(plan["evidence_path"])
    _check_evidence_parent(str(target))
    payload_bytes = (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode()
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload_bytes)
    except OSError as exc:
        raise CommitUncertain(
            "The canary read back exactly but its O_EXCL evidence could not be written; do not retry."
        ) from exc
    return event_id


def post_canary_backup(plan: dict[str, Any], plan_path: str) -> dict[str, Any]:
    evidence = json.loads(Path(plan["evidence_path"]).read_text(encoding="utf-8"))
    expected_plan_sha = hashlib.sha256(Path(plan_path).read_bytes()).hexdigest()
    if (
        not isinstance(evidence, dict)
        or evidence.get("schema") != "repoctl.m010028.gate-b-canary-evidence.v1"
        or evidence.get("gate_b_plan_sha256") != expected_plan_sha
        or evidence.get("event", {}).get("event_id") != evidence.get("event_id")
    ):
        raise GateBError("The canary evidence does not bind this Gate B plan and event.")
    gate_a_plan, _ = gate_a.load_plan(plan["gate_a_plan_path"])
    gate_a.check_publisher_binding(gate_a_plan)
    record = gate_a.check_recovery_point(gate_a_plan)
    try:
        event_time = datetime.fromisoformat(evidence["event"]["timestamp"].replace("Z", "+00:00"))
        backup_time = datetime.fromisoformat(record["verified_at_utc"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise GateBError("Canary or recovery evidence has an invalid timestamp.") from exc
    if backup_time < event_time:
        raise GateBError("The verified recovery point predates the committed canary.")
    return record


def restore_canary_backup(plan: dict[str, Any], plan_path: str) -> Path:
    record = post_canary_backup(plan, plan_path)
    evidence = json.loads(Path(plan["evidence_path"]).read_text(encoding="utf-8"))
    event = evidence["event"]
    gate_a_plan, _ = gate_a.load_plan(plan["gate_a_plan_path"])
    environment = gate_a._backup_environment(gate_a_plan)
    restore_name = f"gate-b-{evidence['event_id']}"
    restored = Path(environment["REPOCTL_BACKUP_RESTORE_ROOT"]) / restore_name
    if restored.exists():
        raise GateBError("The separate Gate B restore target already exists; do not overwrite or repeat it.")
    command = [
        gate_a.PINNED_PYTHON, "-m", "repoctl.backup", "restore",
        "--manifest", record["manifest_path"], "--restore-name", restore_name,
    ]
    result = subprocess.run(
        command, env={**os.environ, **environment}, capture_output=True, text=True,
        timeout=300, check=False,
    )
    if result.returncode != 0 or not restored.is_file():
        raise GateBError("The separate post-canary restore failed; preserve evidence and do not rerun the canary.")
    uri = f"file:{restored}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        row = connection.execute(
            "SELECT event_type, payload_json FROM workflow_event "
            "WHERE repository_id = ? AND milestone_id = ? AND event_id = ?",
            (
                gate_a_plan["expected_binding"]["repository_id"],
                gate_a_plan["expected_binding"]["milestone_id"],
                evidence["event_id"],
            ),
        ).fetchone()
    if row is None or row[0] != "test_result_recorded":
        raise GateBError("The separate restored database does not contain the exact canary event.")
    try:
        payload = json.loads(row[1])
    except (TypeError, json.JSONDecodeError) as exc:
        raise GateBError("The restored canary payload is invalid.") from exc
    for key in ("event_id", "event_type", "summary", "evidence_source", "result"):
        if payload.get(key) != event.get(key):
            raise GateBError(f"The restored canary payload differs at {key}.")
    return restored


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M010.028 Gate B controls")
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("validate", "review"):
        actions.add_parser(name).add_argument("--plan", required=True)
    build = actions.add_parser("build-plan")
    build.add_argument("--gate-a-plan", required=True)
    build.add_argument("--window-start", required=True)
    build.add_argument("--window-end", required=True)
    build.add_argument("--nonce", required=True)
    build.add_argument("--evidence-path", required=True)
    build.add_argument("--out", required=True)
    record = actions.add_parser("record-approval")
    record.add_argument("--plan", required=True)
    record.add_argument("--text-file", required=True)
    record.add_argument("--out-dir", required=True)
    for name in (
        "verify-approval", "preflight", "release", "run-canary", "backup-service",
        "post-canary-backup", "restore-canary-backup",
    ):
        action = actions.add_parser(name)
        action.add_argument("--plan", required=True)
        action.add_argument("--approval-record", required=True)
        action.add_argument("--approval-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "build-plan":
            if re.fullmatch(r"[0-9a-f]{32}", args.nonce) is None:
                raise GateBError("--nonce must be 32 lowercase hexadecimal characters.")
            plan = build_plan(
                args.gate_a_plan, args.window_start, args.window_end, args.nonce, args.evidence_path
            )
            payload = (json.dumps(plan, indent=2, sort_keys=True) + "\n").encode()
            fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
            print(f"GATE B PLAN BUILT {args.out} sha256={hashlib.sha256(payload).hexdigest()}")
            return 0
        plan, plan_sha = load_plan(args.plan)
        if args.action == "validate":
            print(f"PLAN VALID {plan_sha}")
        elif args.action == "review":
            sys.stdout.write(render_review(plan, plan_sha))
        elif args.action == "record-approval":
            target, digest = record_approval(args.plan, args.text_file, args.out_dir)
            print(f"APPROVAL RECORDED {target} sha256={digest}")
        else:
            plan = verify_approval(args.plan, args.approval_record, args.approval_sha256)
            if args.action == "verify-approval":
                print("PASS exact Product Owner Gate B approval for this plan, window, release and canary")
            elif args.action == "preflight":
                preflight(plan)
                print("PASS held target, source, coverage, publisher and live managed binding")
            elif args.action == "release":
                release(plan)
                print("RELEASE VERIFIED: exact held paths are writable for the one approved canary")
            elif args.action == "run-canary":
                event_id = run_canary(plan, args.plan)
                print(f"CANARY VERIFIED event_id={event_id}; do not run again")
            elif args.action == "backup-service":
                gate_a_plan, _ = gate_a.load_plan(plan["gate_a_plan_path"])
                print(gate_a_plan["publisher"]["backup_service"])
            elif args.action == "post-canary-backup":
                record = post_canary_backup(plan, args.plan)
                print(f"POST-CANARY BACKUP VERIFIED at {record['verified_at_utc']}")
            elif args.action == "restore-canary-backup":
                restored = restore_canary_backup(plan, args.plan)
                print(f"POST-CANARY RESTORE VERIFIED exact event at {restored}")
        return 0
    except CommitUncertain as exc:
        print(f"COMMIT UNCERTAIN: {exc}", file=sys.stderr)
        return 4
    except (GateBError, gate_a.GateAError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"FAIL {args.action}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
