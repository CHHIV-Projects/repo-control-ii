"""M010.028 Gate A plan, approval, preflight, readiness and publisher checks.

The review path (``validate`` and ``review``) reads only the plan file and
prints data. It never executes programs or opens sockets. Operator execution
lives in ``gate-a-switch.sh``, which refuses before any privileged step unless
``verify-approval`` accepts an exact Product Owner approval record for this
plan and its current window.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import grp
import pwd
import re
import shlex
import socket
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PLAN_SCHEMA = "repoctl.m010028.gate-a-plan.v1"
APPROVAL_SCHEMA = "repoctl.m010028.gate-a-approval.v1"
MAX_WINDOW_SECONDS = 4 * 3600
PINNED_PYTHON = "/home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python"

_SHA = r"[0-9a-f]{64}"
_HEX32 = r"[0-9a-f]{32}"
_ABS = r"/(?:[A-Za-z0-9._@+-]+/)*[A-Za-z0-9._@+-]+"
_REL = r"(?:[A-Za-z0-9._@+-]+/)*[A-Za-z0-9._@+-]+"
_UTC = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
_SERVICE = r"[a-z0-9][a-z0-9-]*\.service"
_TIMER = r"[a-z0-9][a-z0-9-]*\.timer"
_HEALTH_URL = r"http://127\.0\.0\.1:(\d{2,5})/healthz"

BINDING_FIELDS: dict[str, Any] = {
    "authority": r"sqlite_managed",
    "authority_configuration_sha256": _SHA,
    "authority_generation": _HEX32,
    "authority_state_root": _ABS,
    "database_identity": _SHA,
    "milestone_id": r"M\d{3}\.\d{3}",
    "prompt_path": _REL,
    "prompt_revision_id": _HEX32,
    "prompt_sha256": _SHA,
    "repository_id": r"[a-z0-9-]+--[0-9a-f]{12}",
    "repository_key": r"[a-z0-9-]+",
    "repository_root": _ABS,
    "selection_id": _HEX32,
    "selection_revision": int,
}

PUBLISHER_FIELDS: dict[str, Any] = {
    "backup_env_source": _ABS,
    "backup_env_sha256": _SHA,
    "backup_env_path": _ABS,
    "backup_service": _SERVICE,
    "backup_service_sha256": _SHA,
    "monitor_service": _SERVICE,
    "monitor_service_sha256": _SHA,
    "backup_timer": _TIMER,
    "backup_timer_sha256": _SHA,
    "monitor_timer": _TIMER,
    "monitor_timer_sha256": _SHA,
    "allowed_write_roots": list,
}

PLAN_FIELDS: dict[str, Any] = {
    "schema": re.escape(PLAN_SCHEMA),
    "milestone_id": r"M\d{3}\.\d{3}",
    "candidate_label": r"R\d+",
    "window_start_utc": _UTC,
    "window_end_utc": _UTC,
    "unit": _SERVICE,
    "installed_unit_path": _ABS,
    "legacy_unit_sha256": _SHA,
    "legacy_saved_unit_path": _ABS,
    "managed_unit_source": _ABS,
    "managed_unit_sha256": _SHA,
    "managed_env_source": _ABS,
    "managed_env_sha256": _SHA,
    "managed_env_path": _ABS,
    "health_url": _HEALTH_URL,
    "readiness_timeout_seconds": int,
    "candidate_root": _ABS,
    "held_sha256_file": _ABS,
    "held_inventory_file": _ABS,
    "registry_path": _ABS,
    "source_state_root": _ABS,
    "source_inventory_sha256": _SHA,
    "registry_binding_sha256": _SHA,
    "switch_script_sha256": _SHA,
    "return_script_sha256": _SHA,
    "expected_binding": BINDING_FIELDS,
    "publisher": PUBLISHER_FIELDS,
}


class GateAError(Exception):
    pass


def sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _parse_utc(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _check_fields(value: Any, spec: dict[str, Any], where: str) -> None:
    if not isinstance(value, dict):
        raise GateAError(f"{where} must be an object.")
    if set(value) != set(spec):
        extra = sorted(set(value) - set(spec))
        missing = sorted(set(spec) - set(value))
        raise GateAError(f"{where} fields differ from the schema (extra={extra}, missing={missing}).")
    for key, rule in spec.items():
        item = value[key]
        name = f"{where}.{key}"
        if isinstance(rule, dict):
            _check_fields(item, rule, name)
        elif rule is int:
            if type(item) is not int or item < 0:
                raise GateAError(f"{name} must be a non-negative integer.")
        elif rule is list:
            if not isinstance(item, list) or not item or not all(
                isinstance(entry, str) and re.fullmatch(_ABS, entry) and ".." not in entry.split("/")
                for entry in item
            ):
                raise GateAError(f"{name} must be a non-empty list of absolute paths.")
        else:
            if not isinstance(item, str) or not re.fullmatch(rule, item):
                raise GateAError(f"{name} has an invalid value.")
            if rule == _ABS and ".." in item.split("/"):
                raise GateAError(f"{name} must not contain '..'.")


def load_plan(path: str | Path) -> tuple[dict[str, Any], str]:
    raw = Path(path).read_bytes()
    try:
        plan = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateAError(f"The plan is not valid JSON: {exc}") from exc
    _check_fields(plan, PLAN_FIELDS, "plan")
    start, end = _parse_utc(plan["window_start_utc"]), _parse_utc(plan["window_end_utc"])
    if not 0 < (end - start).total_seconds() <= MAX_WINDOW_SECONDS:
        raise GateAError("The plan window must be positive and at most four hours.")
    if not 5 <= plan["readiness_timeout_seconds"] <= 120:
        raise GateAError("readiness_timeout_seconds must be between 5 and 120.")
    if plan["expected_binding"]["authority_state_root"] != f"{plan['candidate_root']}/state":
        raise GateAError("expected_binding.authority_state_root must be <candidate_root>/state.")
    env_name = f"backup-{plan['milestone_id'].lower().replace('.', '')}-{plan['candidate_label'].lower()}.env"
    if Path(plan["publisher"]["backup_env_path"]).name != env_name:
        raise GateAError(f"publisher.backup_env_path must be the candidate-specific file {env_name}.")
    return plan, hashlib.sha256(raw).hexdigest()


def approval_phrase(plan: dict[str, Any], plan_sha256: str) -> str:
    return (
        f"GATE A APPROVED plan_sha256={plan_sha256} "
        f"window={plan['window_start_utc']}/{plan['window_end_utc']}"
    )


def render_review(plan: dict[str, Any], plan_sha256: str) -> str:
    rows: list[tuple[str, str]] = [("plan_sha256", plan_sha256)]

    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for key in sorted(value):
                walk(f"{prefix}.{key}" if prefix else key, value[key])
        elif isinstance(value, list):
            rows.append((prefix, ", ".join(value)))
        else:
            rows.append((prefix, str(value)))

    walk("", plan)
    width = max(len(name) for name, _ in rows)
    lines = [
        "M010.028 GATE A REVIEW (data only; this review cannot switch anything)",
        "",
        *(f"{name.ljust(width)}  {value}" for name, value in rows),
        "",
        "The window end is the latest switch-start deadline, not the maintenance-window end.",
        "Reserve time for readiness and a pre-write return before the agreed safe-stop.",
        "",
        "Approval requires this exact phrase from the Product Owner, inside the window:",
        f"  {approval_phrase(plan, plan_sha256)}",
    ]
    return "\n".join(lines) + "\n"


def record_approval(plan_path: str, text_file: str, out_dir: str) -> tuple[Path, str]:
    plan, plan_sha = load_plan(plan_path)
    text = Path(text_file).read_text(encoding="utf-8")
    if approval_phrase(plan, plan_sha) not in text:
        raise GateAError("The approval text does not contain the exact plan/window approval phrase.")
    if _now() >= _parse_utc(plan["window_end_utc"]):
        raise GateAError("The plan window has already ended.")
    record = {
        "schema": APPROVAL_SCHEMA,
        "plan_sha256": plan_sha,
        "window_start_utc": plan["window_start_utc"],
        "window_end_utc": plan["window_end_utc"],
        "approval_text": text,
        "approval_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "recorded_at_utc": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    payload = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
    target = Path(out_dir) / f"gate-a-approval-{plan_sha[:16]}.json"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
    return target, hashlib.sha256(payload).hexdigest()


def verify_approval(
    plan_path: str, record_path: str, record_sha256: str, switch_script: str, return_script: str
) -> dict[str, Any]:
    plan, plan_sha = load_plan(plan_path)
    raw = Path(record_path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != record_sha256:
        raise GateAError("The approval record does not match the supplied approval SHA-256.")
    record = json.loads(raw)
    expected_keys = {
        "schema", "plan_sha256", "window_start_utc", "window_end_utc",
        "approval_text", "approval_text_sha256", "recorded_at_utc",
    }
    if not isinstance(record, dict) or set(record) != expected_keys or record["schema"] != APPROVAL_SCHEMA:
        raise GateAError("The approval record does not match the approval schema.")
    if record["plan_sha256"] != plan_sha:
        raise GateAError("The approval record names a different plan.")
    if (record["window_start_utc"], record["window_end_utc"]) != (
        plan["window_start_utc"], plan["window_end_utc"]
    ):
        raise GateAError("The approval record names a different window.")
    text = record["approval_text"]
    if (
        hashlib.sha256(text.encode("utf-8")).hexdigest() != record["approval_text_sha256"]
        or approval_phrase(plan, plan_sha) not in text
    ):
        raise GateAError("The approval text is not the exact plan/window approval.")
    check_window(plan)
    if sha256_file(switch_script) != plan["switch_script_sha256"]:
        raise GateAError("The switch script differs from the reviewed plan.")
    if sha256_file(return_script) != plan["return_script_sha256"]:
        raise GateAError("The return script differs from the reviewed plan.")
    return plan


def check_window(plan: dict[str, Any]) -> None:
    if not _parse_utc(plan["window_start_utc"]) <= _now() < _parse_utc(plan["window_end_utc"]):
        raise GateAError("The current time is outside the plan window.")


def systemctl_show(unit: str, properties: list[str], *, timeout: float = 10) -> dict[str, str]:
    args = ["systemctl", "show", unit, *(f"--property={name}" for name in properties)]
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode != 0:
        raise GateAError(f"systemctl show {unit} failed: {result.stderr.strip()}")
    values = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key] = value
    return values


def _require_file(path: str, uid: int, gid: int | None, mode: int, label: str) -> None:
    info = os.lstat(path)
    if (
        not stat.S_ISREG(info.st_mode) or info.st_uid != uid
        or (gid is not None and info.st_gid != gid) or stat.S_IMODE(info.st_mode) != mode
    ):
        raise GateAError(f"{label} must be a regular file owned by uid {uid} with mode {mode:o}.")


class CommitUncertain(GateAError):
    """A protected held path changed or could not be verified."""


def _held_database(plan: dict[str, Any]) -> tuple[str, str, str]:
    database = f"{plan['candidate_root']}/prepared/managed-prepared.sqlite3"
    return database, f"{database}-wal", f"{database}-shm"


def check_held(plan: dict[str, Any]) -> str:
    """Classify the hold as UNCHANGED, EXPECTED_ACTIVITY, or raise CommitUncertain.

    A commit in WAL mode must change the WAL (new frames or a new header salt)
    or the database file, and a rollback-journal write would add a path. The
    WAL-index (-shm) is rewritten by ordinary readers, so only its content may
    differ. Every path's type, owner, mode, and inode must be unchanged, and
    no path may appear or disappear.
    """
    database, wal, shm = _held_database(plan)
    entries: list[tuple[str, str]] = []
    for line in Path(plan["held_sha256_file"]).read_text(encoding="utf-8").splitlines():
        digest, sep, name = line.partition("  ")
        if not sep or not re.fullmatch(_SHA, digest):
            raise GateAError("The held SHA-256 file is malformed.")
        entries.append((name, digest))
    names = {name for name, _ in entries}
    if database not in names or wal not in names:
        raise GateAError("The held SHA-256 file must cover the candidate database and WAL.")
    activity: list[str] = []
    for name, digest in entries:
        try:
            actual = sha256_file(name)
        except OSError as exc:
            raise CommitUncertain(f"cannot read held path {name}: {exc.strerror}") from exc
        if actual == digest:
            continue
        if name == shm:
            activity.append("WAL-index (-shm) content changed")
            continue
        raise CommitUncertain(f"protected held content changed: {name}")
    result = subprocess.run(
        ["find", plan["candidate_root"], "-xdev", "-printf", r"%p|%y|%u:%g|%m|%d:%i\n"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode != 0:
        raise CommitUncertain("the held path inventory could not be read completely")
    current = set(result.stdout.splitlines())
    expected = set(Path(plan["held_inventory_file"]).read_text(encoding="utf-8").splitlines())
    if current != expected:
        changed = sorted({line.split("|", 1)[0] for line in current ^ expected})
        raise CommitUncertain(f"held path inventory changed: {', '.join(changed[:5])}")
    if activity:
        return "EXPECTED_ACTIVITY " + "; ".join(activity) + "; database, WAL, and inventory unchanged"
    return "UNCHANGED held hashes and inventory"


def check_source_parity(plan: dict[str, Any]) -> None:
    from repoctl.migration_rehearsal import _managed_registered_source_snapshot
    from repoctl.web.repository_registry import RepositoryRegistry

    registry = RepositoryRegistry.from_toml(plan["registry_path"])
    _, _, source_hash, registry_hash = _managed_registered_source_snapshot(
        registry, Path(plan["source_state_root"])
    )
    if source_hash != plan["source_inventory_sha256"]:
        raise GateAError("Source parity failed: registered sources changed since capture.")
    if registry_hash != plan["registry_binding_sha256"]:
        raise GateAError("Registry binding parity failed.")


BACKUP_ENV_PATHS = (
    "REPOCTL_BACKUP_SOURCE", "REPOCTL_BACKUP_STAGING", "REPOCTL_BACKUP_DESTINATION",
    "REPOCTL_BACKUP_RESTORE_ROOT", "REPOCTL_BACKUP_MOUNTPOINT", "REPOCTL_BACKUP_COVERAGE_PATH",
    "REPOCTL_BACKUP_AUTHORITY_STATE_ROOT", "REPOCTL_BACKUP_REPOSITORY_ROOT",
)
BACKUP_ENV_RULES: dict[str, str] = {
    **{key: _ABS for key in BACKUP_ENV_PATHS},
    "REPOCTL_BACKUP_MOUNT_SOURCE": r"//[A-Za-z0-9.-]+/[A-Za-z0-9._-]+",
    "REPOCTL_BACKUP_REPOSITORY_ID": r"[a-z0-9][a-z0-9._-]*--[0-9a-f]{12}",
    "REPOCTL_BACKUP_REPOSITORY_KEY": r"[a-z0-9][a-z0-9._-]*",
    "REPOCTL_BACKUP_MAX_AGE_SECONDS": r"[1-9][0-9]{1,5}",
}


def _backup_environment(plan: dict[str, Any]) -> dict[str, str]:
    """Parse the reviewed backup env, admitting only approved non-secret bindings."""
    values: dict[str, str] = {}
    for line in Path(plan["publisher"]["backup_env_source"]).read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or key not in BACKUP_ENV_RULES or key in values:
            raise GateAError(f"The backup environment has an unapproved or repeated entry: {key or line[:20]}")
        if not re.fullmatch(BACKUP_ENV_RULES[key], value) or (
            BACKUP_ENV_RULES[key] == _ABS and ".." in value.split("/")
        ):
            raise GateAError(f"The backup environment value for {key} is not an approved binding.")
        values[key] = value
    missing = sorted(set(BACKUP_ENV_RULES) - set(values))
    if missing:
        raise GateAError(f"The backup environment is missing {', '.join(missing)}.")
    return values


def _require_root_controlled_directory(path: str) -> None:
    """Every ancestor must be a root-owned real directory writable only by root."""
    current = Path(path).parent
    while True:
        info = os.lstat(current)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise GateAError(f"{current} is not a root-controlled directory.")
        if current == current.parent:
            return
        current = current.parent


def check_recovery_point(plan: dict[str, Any]) -> dict[str, Any]:
    from repoctl.backup import _load_active_authority, _verify_managed_coverage, config_from_environment

    environment = _backup_environment(plan)
    configuration = config_from_environment({**os.environ, **environment})
    authority = _load_active_authority(configuration)
    if authority is None:
        raise GateAError("The candidate has no active managed authority.")
    coverage_key = "REPOCTL_BACKUP_COVERAGE_PATH"
    previous_coverage = os.environ.get(coverage_key)
    # Opening the managed store also reads the process environment.
    os.environ[coverage_key] = environment[coverage_key]
    try:
        record = _verify_managed_coverage(configuration, authority)
    finally:
        if previous_coverage is None:
            os.environ.pop(coverage_key, None)
        else:
            os.environ[coverage_key] = previous_coverage
    bound = record["authority_binding"]
    expected = plan["expected_binding"]
    if (
        bound["authority"]["generation"] != expected["authority_generation"]
        or bound["database_identity"] != expected["database_identity"]
        or bound["selection"]["selection_id"] != expected["selection_id"]
        or bound["selection"]["selection_revision"] != expected["selection_revision"]
    ):
        raise GateAError("The verified recovery point is bound to a different candidate.")
    return record


def check_publisher_binding(plan: dict[str, Any]) -> None:
    """Prove the backup/monitor publishers bind only to this candidate."""
    publisher = plan["publisher"]
    if sha256_file(publisher["backup_env_source"]) != publisher["backup_env_sha256"]:
        raise GateAError("The reviewed backup environment source changed.")
    env = _backup_environment(plan)
    binding = plan["expected_binding"]
    expected_env = {
        "REPOCTL_BACKUP_SOURCE": f"{plan['candidate_root']}/prepared/managed-prepared.sqlite3",
        "REPOCTL_BACKUP_AUTHORITY_STATE_ROOT": binding["authority_state_root"],
        "REPOCTL_BACKUP_REPOSITORY_ID": binding["repository_id"],
        "REPOCTL_BACKUP_REPOSITORY_KEY": binding["repository_key"],
        "REPOCTL_BACKUP_REPOSITORY_ROOT": binding["repository_root"],
    }
    for key, value in expected_env.items():
        if env.get(key) != value:
            raise GateAError(f"Backup environment {key} is not bound to this candidate.")
    coverage = env.get("REPOCTL_BACKUP_COVERAGE_PATH", "")
    if not coverage or Path(coverage).parent != Path(env.get("REPOCTL_BACKUP_STAGING", "")):
        raise GateAError("The coverage path is not in this candidate's backup staging directory.")
    if sha256_file(plan["managed_env_source"]) != plan["managed_env_sha256"]:
        raise GateAError("The reviewed managed environment source changed.")
    managed_env = Path(plan["managed_env_source"]).read_text(encoding="utf-8").splitlines()
    if f"REPOCTL_BACKUP_COVERAGE_PATH={coverage}" not in managed_env:
        raise GateAError("The managed browser coverage path differs from the backup publisher.")

    installed = publisher["backup_env_path"]
    _require_root_controlled_directory(installed)
    _require_file(installed, 0, os.getgid(), 0o640, "The installed backup environment")
    if sha256_file(installed) != publisher["backup_env_sha256"]:
        raise GateAError("The installed backup environment differs from the reviewed source.")

    candidate_root = plan["candidate_root"]
    allowed = [candidate_root, *publisher["allowed_write_roots"]]
    for name in ("backup", "monitor"):
        unit = publisher[f"{name}_service"]
        show = systemctl_show(
            unit, ["FragmentPath", "DropInPaths", "EnvironmentFiles", "ReadWritePaths", "ReadOnlyPaths"]
        )
        fragment = show.get("FragmentPath") or "/nonexistent"
        if not Path(fragment).is_file() or sha256_file(fragment) != publisher[f"{name}_service_sha256"]:
            raise GateAError(f"{unit} is not the reviewed unit.")
        if show.get("DropInPaths"):
            raise GateAError(f"{unit} has drop-ins.")
        if show.get("EnvironmentFiles", "").split(" ")[0] != installed:
            raise GateAError(f"{unit} does not use the reviewed backup environment.")
        for path in show.get("ReadWritePaths", "").split():
            path = path.lstrip("-")
            if not any(path == root or path.startswith(f"{root}/") for root in allowed):
                raise GateAError(f"{unit} may write outside this candidate: {path}")
        for path in f"{show.get('ReadWritePaths', '')} {show.get('ReadOnlyPaths', '')}".split():
            path = path.lstrip("-")
            if "/repo-control-staging/" in path and not (
                path == candidate_root or path.startswith(f"{candidate_root}/")
            ):
                raise GateAError(f"{unit} references another candidate: {path}")
    for name in ("backup", "monitor"):
        timer = publisher[f"{name}_timer"]
        fragment = systemctl_show(timer, ["FragmentPath"]).get("FragmentPath") or "/nonexistent"
        if not Path(fragment).is_file() or sha256_file(fragment) != publisher[f"{name}_timer_sha256"]:
            raise GateAError(f"{timer} is not the reviewed timer.")

    own = {publisher[f"{name}_{kind}"] for name in ("backup", "monitor") for kind in ("service", "timer")}
    listing = subprocess.run(
        ["systemctl", "list-unit-files", "--no-legend", "--plain", "repo-control-backup*"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if listing.returncode != 0:
        raise GateAError("Could not list backup publisher units.")
    for line in listing.stdout.splitlines():
        fields = line.split()
        if not fields or fields[0] in own:
            continue
        state = systemctl_show(fields[0], ["ActiveState", "UnitFileState"])
        if state.get("ActiveState") not in ("inactive", "failed") or state.get("UnitFileState") == "enabled":
            raise GateAError(f"Another backup publisher is running or enabled: {fields[0]}")


def check_saved_legacy(plan: dict[str, Any]) -> None:
    _require_file(plan["legacy_saved_unit_path"], 0, 0, 0o644, "The saved legacy unit")
    if sha256_file(plan["legacy_saved_unit_path"]) != plan["legacy_unit_sha256"]:
        raise GateAError("The saved legacy unit differs from the plan.")


def _unit_directives(path: str) -> dict[str, dict[str, list[str]]]:
    """Read the small reviewed unit without asking systemd to execute or load it."""
    sections: dict[str, dict[str, list[str]]] = {}
    section: str | None = None
    logical = ""
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        logical += line[:-1] if line.endswith("\\") else line
        if line.endswith("\\"):
            continue
        line, logical = logical.strip(), ""
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            sections.setdefault(section, {})
            continue
        key, separator, value = line.partition("=")
        if section is None or not separator or not key:
            raise GateAError(f"The managed unit has malformed content: {line[:80]}")
        sections[section].setdefault(key, []).append(value.strip())
    if logical:
        raise GateAError("The managed unit ends with an incomplete continuation.")
    return sections


def _one(directives: dict[str, list[str]], name: str) -> str:
    values = directives.get(name, [])
    if len(values) != 1 or not values[0]:
        raise GateAError(f"The managed unit must define exactly one {name} directive.")
    return values[0]


def _argument_value(arguments: list[str], name: str) -> str:
    positions = [index for index, value in enumerate(arguments) if value == name]
    if len(positions) != 1 or positions[0] + 1 >= len(arguments):
        raise GateAError(f"The managed ExecStart must define exactly one {name} value.")
    return arguments[positions[0] + 1]


def _require_real_path(path: str, *, directory: bool, label: str) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise GateAError(f"The managed unit {label} does not exist: {path}") from exc
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if stat.S_ISLNK(info.st_mode) or not expected:
        kind = "directory" if directory else "regular file"
        raise GateAError(f"The managed unit {label} must be a real {kind}: {path}")


def check_managed_unit_contract(plan: dict[str, Any]) -> None:
    """Fail before service stop if the reviewed unit cannot enter its sandbox.

    This binds the paths used by ExecStart to the paths admitted by systemd.
    In particular, every ReadWritePaths target must already exist because a
    missing target makes systemd fail with 226/NAMESPACE before Python starts.
    """
    parsed = _unit_directives(plan["managed_unit_source"])
    service = parsed.get("Service", {})
    unit = parsed.get("Unit", {})
    try:
        arguments = shlex.split(_one(service, "ExecStart"), posix=True)
    except ValueError as exc:
        raise GateAError(f"The managed ExecStart cannot be parsed: {exc}") from exc
    if arguments[:4] != [PINNED_PYTHON, "-m", "repoctl.cli", "web"]:
        raise GateAError("The managed ExecStart does not use the reviewed pinned Repo Control runtime.")
    if _one(service, "WorkingDirectory") != plan["expected_binding"]["repository_root"]:
        raise GateAError("The managed WorkingDirectory differs from the planned repository root.")
    if _one(service, "EnvironmentFile").lstrip("-") != plan["managed_env_path"]:
        raise GateAError("The managed EnvironmentFile differs from the planned installed environment.")
    if _one(service, "EnvironmentFile").startswith("-"):
        raise GateAError("The managed EnvironmentFile must not be optional.")
    if _argument_value(arguments, "--repository-registry") != plan["registry_path"]:
        raise GateAError("The managed registry path differs from the plan.")
    if _argument_value(arguments, "--state-root") != plan["expected_binding"]["authority_state_root"]:
        raise GateAError("The managed state root differs from the planned authority binding.")
    if _argument_value(arguments, "--host") != "127.0.0.1":
        raise GateAError("The managed service must bind only to loopback.")
    port = re.fullmatch(_HEALTH_URL, plan["health_url"])
    if port is None or _argument_value(arguments, "--port") != port.group(1):
        raise GateAError("The managed service port differs from the planned health endpoint.")

    scanner = _argument_value(arguments, "--scanner-cache-root")
    if Path(scanner).name != "scanner" or Path(scanner).parent.name != Path(plan["candidate_root"]).name:
        raise GateAError("The scanner cache is not candidate-specific or does not end in /scanner.")
    database, wal, shm = _held_database(plan)
    state_locks = f"{plan['expected_binding']['authority_state_root']}/.locks"
    expected_writes = {database, wal, shm, state_locks, scanner}
    write_paths: list[str] = []
    for value in service.get("ReadWritePaths", []):
        try:
            write_paths.extend(shlex.split(value, posix=True))
        except ValueError as exc:
            raise GateAError(f"The managed ReadWritePaths cannot be parsed: {exc}") from exc
    if any(value.startswith("-") for value in write_paths):
        raise GateAError("The managed ReadWritePaths must not make required paths optional.")
    if set(write_paths) != expected_writes:
        missing = sorted(expected_writes - set(write_paths))
        extra = sorted(set(write_paths) - expected_writes)
        raise GateAError(f"The managed ReadWritePaths differ from the runtime contract (missing={missing}, extra={extra}).")
    for path in (database, wal, shm):
        _require_real_path(path, directory=False, label="database path")
    _require_real_path(state_locks, directory=True, label="state lock directory")
    _require_real_path(scanner, directory=True, label="scanner cache directory")
    try:
        service_uid = pwd.getpwnam(_one(service, "User")).pw_uid
        service_gid = grp.getgrnam(_one(service, "Group")).gr_gid
    except KeyError as exc:
        raise GateAError(f"The managed service identity does not exist: {exc}") from exc
    scanner_info = os.lstat(scanner)
    if (
        scanner_info.st_uid != service_uid
        or scanner_info.st_gid != service_gid
        or stat.S_IMODE(scanner_info.st_mode) != 0o700
    ):
        raise GateAError("The scanner cache must be owned by the managed service user/group with mode 0700.")

    required_hardening = {
        "NoNewPrivileges": "true",
        "PrivateTmp": "true",
        "ProtectHome": "read-only",
        "ProtectSystem": "strict",
        "Restart": "on-failure",
    }
    for name, expected in required_hardening.items():
        if _one(service, name).lower() != expected:
            raise GateAError(f"The managed unit must set {name}={expected}.")
    if _one(unit, "StartLimitBurst") != "1":
        raise GateAError("The managed unit must limit initial live-start failures to one attempt.")


def sandbox_probe(plan: dict[str, Any]) -> None:
    """Exercise the reviewed namespace in a transient one-shot without starting Repo Control."""
    if os.geteuid() != 0:
        raise GateAError("The transient systemd sandbox probe must run as root.")
    check_managed_unit_contract(plan)
    parsed = _unit_directives(plan["managed_unit_source"])
    service = parsed["Service"]
    write_paths: list[str] = []
    for value in service["ReadWritePaths"]:
        write_paths.extend(shlex.split(value, posix=True))
    probe_unit = (
        f"repo-control-{plan['milestone_id'].lower().replace('.', '')}-"
        f"{plan['candidate_label'].lower()}-sandbox-probe"
    )
    command = [
        "systemd-run", "--quiet", "--wait", "--collect", f"--unit={probe_unit}",
        "--property=Type=oneshot", f"--property=User={_one(service, 'User')}",
        f"--property=Group={_one(service, 'Group')}",
        f"--property=WorkingDirectory={_one(service, 'WorkingDirectory')}",
        "--property=Restart=no",
        f"--property=NoNewPrivileges={_one(service, 'NoNewPrivileges')}",
        f"--property=PrivateTmp={_one(service, 'PrivateTmp')}",
        f"--property=ProtectHome={_one(service, 'ProtectHome')}",
        f"--property=ProtectSystem={_one(service, 'ProtectSystem')}",
        f"--property=ReadWritePaths={' '.join(write_paths)}",
        "/usr/bin/true",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise GateAError(f"The transient systemd sandbox probe failed: {detail}")


def preflight(plan: dict[str, Any]) -> list[str]:
    check_window(plan)
    check_saved_legacy(plan)
    if sha256_file(plan["installed_unit_path"]) != plan["legacy_unit_sha256"]:
        raise GateAError("The installed unit is not the expected legacy unit.")
    show = systemctl_show(plan["unit"], ["ActiveState", "FragmentPath", "DropInPaths"])
    if show.get("FragmentPath") != plan["installed_unit_path"] or show.get("DropInPaths"):
        raise GateAError("The live unit path differs from the plan or has drop-ins.")
    if sha256_file(plan["managed_unit_source"]) != plan["managed_unit_sha256"]:
        raise GateAError("The managed unit source differs from the plan.")
    if sha256_file(plan["managed_env_source"]) != plan["managed_env_sha256"]:
        raise GateAError("The managed environment source differs from the plan.")
    check_managed_unit_contract(plan)
    held = check_held(plan)
    check_publisher_binding(plan)
    check_source_parity(plan)
    record = check_recovery_point(plan)
    check_window(plan)
    return [
        "PASS window, legacy unit, reviewed managed sources and runtime path contract",
        f"PASS {held}",
        "PASS source and registry parity",
        f"PASS recovery point verified at {record['verified_at_utc']}",
        "PASS publisher binding",
    ]


class _DeadlineSocket(socket.socket):
    def __init__(self, deadline: float) -> None:
        super().__init__(socket.AF_INET, socket.SOCK_STREAM)
        self.deadline = deadline

    def set_remaining_timeout(self) -> None:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Health request deadline expired.")
        self.settimeout(remaining)

    def recv_into(self, buffer: Any, nbytes: int = 0, flags: int = 0) -> int:
        self.set_remaining_timeout()
        return super().recv_into(buffer, nbytes, flags)


def _fetch_health(url: str, deadline: float) -> dict[str, Any] | None:
    match = re.fullmatch(_HEALTH_URL, url)
    if match is None:
        raise GateAError("The health URL must be the planned loopback endpoint.")
    port = int(match.group(1))
    connection = http.client.HTTPConnection("127.0.0.1", port)
    try:
        with _DeadlineSocket(deadline) as sock:
            sock.set_remaining_timeout()
            sock.connect(("127.0.0.1", port))
            connection.sock = sock
            sock.set_remaining_timeout()
            connection.request("GET", "/healthz")
            with connection.getresponse() as response:
                if not 200 <= response.status < 300:
                    return None
                health = json.loads(response.read(1_000_000))
                return health if isinstance(health, dict) else None
    except (http.client.HTTPException, OSError, ValueError):
        return None
    finally:
        connection.close()


def readiness(plan: dict[str, Any], expect: str) -> str:
    """Poll until the unit and health prove the intended authority; fail on any mismatch or timeout."""
    unit_sha = plan["managed_unit_sha256"] if expect == "managed" else plan["legacy_unit_sha256"]
    # Reserve part of the plan budget for timed-out operations to unwind.
    deadline = time.monotonic() + plan["readiness_timeout_seconds"] - 0.1
    state = "unknown"
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            show = systemctl_show(
                plan["unit"], ["ActiveState", "FragmentPath", "DropInPaths"], timeout=min(10, remaining)
            )
        except subprocess.TimeoutExpired as exc:
            raise GateAError(f"systemctl show {plan['unit']} timed out during readiness.") from exc
        if show.get("FragmentPath") != plan["installed_unit_path"] or show.get("DropInPaths"):
            raise GateAError("The live unit path differs from the plan or has drop-ins.")
        if sha256_file(plan["installed_unit_path"]) != unit_sha:
            raise GateAError(f"The installed unit is not the {expect} unit.")
        state = show.get("ActiveState")
        if state == "failed":
            raise GateAError(f"{plan['unit']} failed to start.")
        health = _fetch_health(plan["health_url"], deadline) if state == "active" else None
        if time.monotonic() >= deadline:
            break
        if health is not None:
            authority = health.get("information_store_authority")
            binding = health.get("information_store_binding")
            if expect == "legacy":
                if authority != "legacy_files" or binding is not None:
                    raise GateAError("Live health does not report legacy_files with a null binding.")
                return "READY legacy_files with null managed binding"
            if authority != "sqlite_managed" or binding != plan["expected_binding"]:
                raise GateAError("Live health does not report the exact planned managed binding.")
            return (
                f"READY sqlite_managed generation={binding['authority_generation']} "
                f"database={binding['database_identity']} "
                f"selection={binding['selection_id']}@{binding['selection_revision']}"
            )
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.5, remaining))
    raise GateAError(f"Readiness timed out (state={state}, health unavailable).")


def source_freeze_inactive(plan: dict[str, Any]) -> None:
    from repoctl.scanner.core import state_artifact_write_freeze_status

    if state_artifact_write_freeze_status(Path(plan["source_state_root"])) is not None:
        raise GateAError("A source freeze remains active.")


def scalar_fields(plan: dict[str, Any], names: list[str]) -> str:
    values = []
    for name in names:
        if name not in PLAN_FIELDS or isinstance(PLAN_FIELDS[name], dict):
            raise GateAError(f"Unknown scalar plan field: {name}")
        values.append(str(plan[name]))
    return " ".join(values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M010.028 Gate A checks")
    actions = parser.add_subparsers(dest="action", required=True)
    for name in (
        "validate", "review", "preflight", "held-check", "publisher-binding", "freeze-inactive",
        "saved-legacy", "sandbox-probe",
    ):
        actions.add_parser(name).add_argument("--plan", required=True)
    field = actions.add_parser("field")
    field.add_argument("--plan", required=True)
    field.add_argument("names", nargs="+")
    approve = actions.add_parser("record-approval")
    approve.add_argument("--plan", required=True)
    approve.add_argument("--text-file", required=True)
    approve.add_argument("--out-dir", required=True)
    verify = actions.add_parser("verify-approval")
    verify.add_argument("--plan", required=True)
    verify.add_argument("--approval-record", required=True)
    verify.add_argument("--approval-sha256", required=True)
    verify.add_argument("--switch-script", required=True)
    verify.add_argument("--return-script", required=True)
    verify.add_argument("--preflight", action="store_true")
    verify.add_argument("--fields", nargs="+")
    ready = actions.add_parser("readiness")
    ready.add_argument("--plan", required=True)
    ready.add_argument("--expect", required=True, choices=("managed", "legacy"))
    args = parser.parse_args(argv)
    try:
        plan, plan_sha = load_plan(args.plan)
        if args.action == "validate":
            print(f"PLAN VALID {plan_sha}")
        elif args.action == "review":
            sys.stdout.write(render_review(plan, plan_sha))
        elif args.action == "field":
            print(scalar_fields(plan, args.names))
        elif args.action == "record-approval":
            target, digest = record_approval(args.plan, args.text_file, args.out_dir)
            print(f"APPROVAL RECORDED {target} sha256={digest}")
        elif args.action == "verify-approval":
            approved_plan = verify_approval(
                args.plan, args.approval_record, args.approval_sha256, args.switch_script, args.return_script
            )
            if args.preflight:
                preflight(approved_plan)
                if verify_approval(
                    args.plan, args.approval_record, args.approval_sha256, args.switch_script, args.return_script
                ) != approved_plan:
                    raise GateAError("The approved plan changed during preflight.")
            if args.fields:
                print(scalar_fields(approved_plan, args.fields))
            else:
                print("PASS exact Product Owner approval for this plan and current window")
        elif args.action == "preflight":
            print("\n".join(preflight(plan)))
        elif args.action == "held-check":
            print(f"PASS {check_held(plan)}")
        elif args.action == "publisher-binding":
            check_publisher_binding(plan)
            print("PASS publishers bind only to this candidate; no other publisher running or enabled")
        elif args.action == "sandbox-probe":
            sandbox_probe(plan)
            print("PASS transient systemd namespace probe; Repo Control application was not started")
        elif args.action == "saved-legacy":
            check_saved_legacy(plan)
            print("PASS saved legacy unit is root:root 0644 with the planned hash")
        elif args.action == "freeze-inactive":
            source_freeze_inactive(plan)
            print("PASS no active source freeze")
        elif args.action == "readiness":
            print(readiness(plan, args.expect))
    except CommitUncertain as exc:
        print(f"COMMIT UNCERTAIN {args.action}: {exc}", file=sys.stderr)
        return 4
    except Exception as exc:  # noqa: BLE001 - every failure must be a clean non-zero result
        print(f"FAIL {args.action}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
