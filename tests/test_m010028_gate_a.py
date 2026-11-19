"""M010.028 Gate A handoff tests.

Everything privileged is replaced by PATH stubs that act only inside a
temporary root, and health is served by a loopback stub. Nothing here calls the
real sudo or systemctl or touches live services.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import socket
import subprocess
import tempfile
import textwrap
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
OPS = REPO / "ops" / "m010028"
GATE = OPS / "gate_a.py"
SWITCH = OPS / "gate-a-switch.sh"
RETURN = OPS / "gate-a-return.sh"
PY = "/home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python"

spec = importlib.util.spec_from_file_location("m010028_gate_a", GATE)
gate_a = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(gate_a)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def utc(delta_minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=delta_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


SUDO_STUB = """#!/bin/bash
echo "sudo $*" >> "$GATEA_STATE/calls.log"
[[ "$1" == "-v" ]] && exit 0
[[ "$1" == "-n" ]] && shift
exec "$@"
"""

INSTALL_STUB = """#!/bin/bash
echo "install $*" >> "$GATEA_STATE/calls.log"
mode=""; args=()
while [[ $# -gt 0 ]]; do
  case "$1" in -o|-g) shift 2;; -m) mode=$2; shift 2;; *) args+=("$1"); shift;; esac
done
src=${args[0]}; dst=${args[1]}
[[ "$dst" == "$GATEA_ROOT"/* ]] || { echo "stub install refused $dst" >&2; exit 9; }
cp "$src" "$dst" && chmod "$mode" "$dst"
"""

SYSTEMCTL_STUB = """#!/usr/bin/env python3
import json, os, sys
state_dir = os.environ["GATEA_STATE"]
with open(os.path.join(state_dir, "calls.log"), "a") as log:
    log.write("systemctl " + " ".join(sys.argv[1:]) + "\\n")
path = os.path.join(state_dir, "systemctl.json")
config = json.load(open(path))
action, rest = sys.argv[1], sys.argv[2:]
if action in ("stop", "start"):
    config["units"][rest[0]]["ActiveState"] = "inactive" if action == "stop" else "active"
    json.dump(config, open(path, "w"))
elif action == "daemon-reload":
    pass
elif action == "show":
    unit = config["units"].get(rest[0], {})
    for arg in rest[1:]:
        name = arg.split("=", 1)[1]
        print(f"{name}={unit.get(name, '')}")
elif action == "list-unit-files":
    if os.path.exists(os.path.join(state_dir, "arm-recovery-after-publisher")):
        open(os.path.join(state_dir, "recovery-stale"), "w").close()
    for name in config["list"]:
        print(f"{name} {config['units'][name].get('UnitFileState', 'static')} -")
else:
    sys.exit(f"unsupported stub action {action}")
"""

SITECUSTOMIZE = """
import json, os
_owned = json.loads(os.environ.get("GATEA_ROOT_OWNED", "{}"))
if _owned:
    _real_lstat = os.lstat
    def _lstat(path, *args, **kwargs):
        info = _real_lstat(path, *args, **kwargs)
        override = _owned.get(os.fspath(path))
        if override is None:
            return info
        values = list(info)
        values[4], values[5] = override[0], override[1]
        if len(override) > 2:
            values[0] = override[2]
        return os.stat_result(values)
    os.lstat = _lstat
if os.environ.get("GATEA_FAKE_REPOCTL"):
    import repoctl.migration_rehearsal as rehearsal
    import repoctl.backup as backup
    import repoctl.scanner.core as scanner
    import repoctl.web.repository_registry as registry
    fake = json.loads(os.environ["GATEA_FAKE_REPOCTL"])
    if os.path.exists(os.path.join(os.environ["GATEA_STATE"], "source-stale")):
        fake["source"] = "0" * 64
    registry.RepositoryRegistry.from_toml = classmethod(lambda cls, path: object())
    def _snapshot(reg, root):
        state = os.environ["GATEA_STATE"]
        marker = os.path.join(state, "restore-approved-plan")
        if os.path.exists(marker):
            import shutil
            shutil.copyfile(os.path.join(state, "approved-plan.json"), os.path.join(state, "..", "plan.json"))
            os.remove(marker)
        return None, None, fake["source"], fake["registry"]
    rehearsal._managed_registered_source_snapshot = _snapshot
    backup.config_from_environment = lambda env: object()
    backup._load_active_authority = lambda configuration: object()
    def _verify_managed_coverage(configuration, authority):
        if os.environ.get("REPOCTL_BACKUP_COVERAGE_PATH") != fake["coverage"]:
            raise backup.BackupError("The candidate coverage path is not in the process environment.")
        if os.path.exists(os.path.join(os.environ["GATEA_STATE"], "recovery-stale")):
            raise backup.BackupError("Fixture recovery point expired.")
        if os.path.exists(os.path.join(os.environ["GATEA_STATE"], "advance-clock-during-coverage")):
            open(os.path.join(os.environ["GATEA_STATE"], "clock-advanced"), "w").close()
        return fake["recovery_point"]
    backup._verify_managed_coverage = _verify_managed_coverage
    scanner.state_artifact_write_freeze_status = lambda root: None
"""


class HealthStub:
    """Serves legacy or managed health depending on which unit file is installed."""

    def __init__(self, fixture: "GateAFixture") -> None:
        self.fixture = fixture
        self.managed_mode = "exact"
        self.on_managed: Path | None = None
        self.response_delay = 0.0
        self.stream_interval = 0.0
        self.redirect_health = False
        self.server: ThreadingHTTPServer | None = None
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()

    def payload(self) -> dict | None:
        installed = sha(self.fixture.installed_unit)
        if installed == self.fixture.legacy_sha:
            return {"information_store_authority": "legacy_files", "information_store_binding": None}
        if self.on_managed is not None:
            self.on_managed.write_bytes(b"frames written by a managed commit")
            self.on_managed = None
        if self.managed_mode == "never":
            return None
        if self.managed_mode == "legacy":
            return {"information_store_authority": "legacy_files", "information_store_binding": None}
        binding = dict(self.fixture.binding)
        if self.managed_mode == "wrong":
            binding["database_identity"] = "0" * 64
        return {"information_store_authority": "sqlite_managed", "information_store_binding": binding}

    def start(self, delay: float = 0.0) -> None:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if stub.redirect_health and self.path == "/healthz":
                    self.send_response(302)
                    self.send_header("Location", "/redirect-target")
                    self.end_headers()
                    return
                body = stub.payload()
                if body is None:
                    self.send_response(503)
                    self.end_headers()
                    return
                time.sleep(stub.response_delay)
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if stub.stream_interval:
                    for byte in data[:40]:
                        try:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            return
                        time.sleep(stub.stream_interval)
                else:
                    try:
                        self.wfile.write(data)
                    except (BrokenPipeError, ConnectionResetError):
                        return

            def log_message(self, *args) -> None:
                pass

        def serve() -> None:
            time.sleep(delay)
            self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
            self.server.serve_forever(poll_interval=0.1)

        threading.Thread(target=serve, daemon=True).start()

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()


class GateAFixture:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.root = base / "root"
        self.state = base / "state"
        self.bin = base / "bin"
        self.site = base / "site"
        for path in (self.root, self.state, self.bin, self.site):
            path.mkdir()
        self.unit = "repo-control-gatea-test.service"
        etc = self.root / "etc"
        (etc / "systemd" / "system").mkdir(parents=True)
        (etc / "repo-control").mkdir()
        self.installed_unit = etc / "systemd" / "system" / self.unit
        self.saved_legacy = etc / "repo-control" / "legacy.service"
        self.saved_legacy.write_text("[Service]\nExecStart=/bin/legacy\n")
        self.saved_legacy.chmod(0o644)
        shutil.copy(self.saved_legacy, self.installed_unit)
        self.legacy_sha = sha(self.saved_legacy)
        self.managed_unit = self.root / "managed.service"
        self.candidate = self.root / "var" / "lib" / "repo-control-staging" / "m010.028-r9"
        prepared = self.candidate / "prepared"
        prepared.mkdir(parents=True)
        (self.candidate / "state" / ".locks").mkdir(parents=True)
        self.lock = self.candidate / "state" / ".locks" / "state-artifacts.lock"
        self.lock.write_bytes(b"")
        self.scanner = self.root / "home" / "chuck" / ".cache" / "repoctl" / "m010.028-r9" / "scanner"
        self.scanner.mkdir(parents=True)
        self.scanner.chmod(0o700)
        for suffix in ("", "-wal", "-shm"):
            (prepared / f"managed-prepared.sqlite3{suffix}").write_bytes(f"db{suffix}".encode())
        self.held_sha = base / "held.sha256"
        self.held_sha.write_text("".join(
            f"{sha(prepared / name)}  {prepared / name}\n"
            for name in ("managed-prepared.sqlite3", "managed-prepared.sqlite3-wal", "managed-prepared.sqlite3-shm")
        ))
        self.inventory = base / "held-inventory.txt"
        listing = subprocess.run(
            ["find", str(self.candidate), "-xdev", "-printf", r"%p|%y|%u:%g|%m|%d:%i\n"],
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        self.inventory.write_text("".join(f"{line}\n" for line in sorted(listing)))
        staging = self.root / "backup-staging" / "r9"
        staging.mkdir(parents=True)
        self.coverage = staging / ".repoctl-verified-recovery-point.json"
        self.managed_env = self.root / "managed.env"
        self.managed_env.write_text(f"REPOCTL_BACKUP_COVERAGE_PATH={self.coverage}\n")
        self.managed_env_path = self.root / "etc" / "repo-control" / "managed.env"
        self.managed_unit.write_text(textwrap.dedent(f"""
            [Unit]
            StartLimitIntervalSec=300
            StartLimitBurst=1

            [Service]
            User=chuck
            Group=chuck
            WorkingDirectory=/home/chuck/projects/repo-control-ii
            EnvironmentFile={self.managed_env_path}
            ExecStart={PY} -m repoctl.cli web --repository-registry {REPO / 'ops' / 'repositories.toml'} --host 127.0.0.1 --port {{PORT}} --state-root {self.candidate}/state --scanner-cache-root {self.scanner}
            Restart=on-failure
            NoNewPrivileges=true
            PrivateTmp=true
            ProtectHome=read-only
            ProtectSystem=strict
            ReadWritePaths={prepared}/managed-prepared.sqlite3 {prepared}/managed-prepared.sqlite3-wal {prepared}/managed-prepared.sqlite3-shm {self.candidate}/state/.locks {self.scanner}
        """).replace("{PORT}", str(self.health.port) if hasattr(self, "health") else "8765"))
        self.binding = {
            "authority": "sqlite_managed",
            "authority_configuration_sha256": "1" * 64,
            "authority_generation": "2" * 32,
            "authority_state_root": f"{self.candidate}/state",
            "database_identity": "3" * 64,
            "milestone_id": "M010.028",
            "prompt_path": "docs/milestones/M010.028_prompt.md",
            "prompt_revision_id": "4" * 32,
            "prompt_sha256": "5" * 64,
            "repository_id": "repo-control-ii--945e7f88eafb",
            "repository_key": "repo-control-ii",
            "repository_root": "/home/chuck/projects/repo-control-ii",
            "selection_id": "6" * 32,
            "selection_revision": 7,
        }
        self.nas = self.root / "nas"
        self.backup_env_source = self.root / "backup.env.source"
        self.backup_env_source.write_text("\n".join([
            "# test publisher binding",
            f"REPOCTL_BACKUP_SOURCE={prepared}/managed-prepared.sqlite3",
            f"REPOCTL_BACKUP_STAGING={staging}",
            f"REPOCTL_BACKUP_DESTINATION={self.root}/nas/RepoControl/repo-control-ii",
            f"REPOCTL_BACKUP_RESTORE_ROOT={self.root}/restore/r9",
            f"REPOCTL_BACKUP_MOUNTPOINT={self.root}/nas",
            "REPOCTL_BACKUP_MOUNT_SOURCE=//192.168.1.171/ProjectBackups",
            "REPOCTL_BACKUP_MAX_AGE_SECONDS=1200",
            f"REPOCTL_BACKUP_COVERAGE_PATH={self.coverage}",
            f"REPOCTL_BACKUP_AUTHORITY_STATE_ROOT={self.candidate}/state",
            "REPOCTL_BACKUP_REPOSITORY_ID=repo-control-ii--945e7f88eafb",
            "REPOCTL_BACKUP_REPOSITORY_KEY=repo-control-ii",
            "REPOCTL_BACKUP_REPOSITORY_ROOT=/home/chuck/projects/repo-control-ii",
        ]) + "\n")
        self.backup_env = etc / "repo-control" / "backup-m010028-r9.env"
        shutil.copy(self.backup_env_source, self.backup_env)
        self.backup_env.chmod(0o640)
        self.units: dict[str, Path] = {}
        for name in ("backup.service", "monitor.service", "backup.timer", "monitor.timer"):
            path = self.root / f"r9-{name}"
            path.write_text(f"# {name}\n")
            self.units[name] = path
        self.root_controlled_env_dir = True
        self.health = HealthStub(self)
        self.managed_unit.write_text(self.managed_unit.read_text().replace("--port 8765", f"--port {self.health.port}"))
        self.plan_path = base / "plan.json"
        self.write_plan()
        self.write_systemctl()
        self.write_stubs()

    def plan(self, **overrides) -> dict:
        plan = {
            "schema": gate_a.PLAN_SCHEMA,
            "milestone_id": "M010.028",
            "candidate_label": "R9",
            "window_start_utc": utc(-5),
            "window_end_utc": utc(55),
            "unit": self.unit,
            "installed_unit_path": str(self.installed_unit),
            "legacy_unit_sha256": self.legacy_sha,
            "legacy_saved_unit_path": str(self.saved_legacy),
            "managed_unit_source": str(self.managed_unit),
            "managed_unit_sha256": sha(self.managed_unit),
            "managed_env_source": str(self.managed_env),
            "managed_env_sha256": sha(self.managed_env),
            "managed_env_path": str(self.managed_env_path),
            "health_url": f"http://127.0.0.1:{self.health.port}/healthz",
            "readiness_timeout_seconds": 5,
            "candidate_root": str(self.candidate),
            "held_sha256_file": str(self.held_sha),
            "held_inventory_file": str(self.inventory),
            "registry_path": str(REPO / "ops" / "repositories.toml"),
            "source_state_root": str(self.root / "source-state"),
            "source_inventory_sha256": "a" * 64,
            "registry_binding_sha256": "b" * 64,
            "switch_script_sha256": sha(SWITCH),
            "return_script_sha256": sha(RETURN),
            "expected_binding": self.binding,
            "publisher": {
                "backup_env_source": str(self.backup_env_source),
                "backup_env_sha256": sha(self.backup_env_source),
                "backup_env_path": str(self.backup_env),
                "backup_service": "repo-control-backup-r9.service",
                "backup_service_sha256": sha(self.units["backup.service"]),
                "monitor_service": "repo-control-backup-monitor-r9.service",
                "monitor_service_sha256": sha(self.units["monitor.service"]),
                "backup_timer": "repo-control-backup-r9.timer",
                "backup_timer_sha256": sha(self.units["backup.timer"]),
                "monitor_timer": "repo-control-backup-monitor-r9.timer",
                "monitor_timer_sha256": sha(self.units["monitor.timer"]),
                "allowed_write_roots": [str(self.coverage.parent), str(self.nas)],
            },
        }
        plan.update(overrides)
        return plan

    def write_plan(self, **overrides) -> str:
        self.plan_path.write_text(json.dumps(self.plan(**overrides), indent=2))
        return sha(self.plan_path)

    def write_systemctl(self, other_timer_state: str = "disabled") -> None:
        candidate = str(self.candidate)
        publisher = {
            "EnvironmentFiles": f"{self.backup_env} (ignore_errors=no)",
            "ReadWritePaths": f"{candidate}/prepared/managed-prepared.sqlite3-shm {self.coverage.parent} {self.nas}",
            "ReadOnlyPaths": f"{candidate}/prepared/managed-prepared.sqlite3 {candidate}/state",
            "DropInPaths": "",
        }
        config = {
            "units": {
                self.unit: {
                    "ActiveState": "active", "FragmentPath": str(self.installed_unit), "DropInPaths": "",
                },
                "repo-control-backup-r9.service": {**publisher, "FragmentPath": str(self.units["backup.service"])},
                "repo-control-backup-monitor-r9.service": {
                    **publisher, "FragmentPath": str(self.units["monitor.service"]),
                },
                "repo-control-backup-r9.timer": {"FragmentPath": str(self.units["backup.timer"])},
                "repo-control-backup-monitor-r9.timer": {"FragmentPath": str(self.units["monitor.timer"])},
                "repo-control-backup-old.timer": {"ActiveState": "inactive", "UnitFileState": other_timer_state},
            },
            "list": [
                "repo-control-backup-r9.service", "repo-control-backup-monitor-r9.service",
                "repo-control-backup-r9.timer", "repo-control-backup-monitor-r9.timer",
                "repo-control-backup-old.timer",
            ],
        }
        (self.state / "systemctl.json").write_text(json.dumps(config))

    def set_publisher(self, **values) -> None:
        path = self.state / "systemctl.json"
        config = json.loads(path.read_text())
        config["units"]["repo-control-backup-r9.service"].update(values)
        path.write_text(json.dumps(config))

    def write_stubs(self) -> None:
        for name, body in (("sudo", SUDO_STUB), ("install", INSTALL_STUB), ("systemctl", SYSTEMCTL_STUB)):
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)
        (self.site / "sitecustomize.py").write_text(SITECUSTOMIZE)

    def env(self) -> dict[str, str]:
        recovery_point = {
            "verified_at_utc": utc(-3),
            "authority_binding": {
                "authority": {"generation": self.binding["authority_generation"]},
                "database_identity": self.binding["database_identity"],
                "selection": {
                    "selection_id": self.binding["selection_id"],
                    "selection_revision": self.binding["selection_revision"],
                },
            },
        }
        owned = {
            str(self.saved_legacy): [0, 0],
            str(self.backup_env): [0, os.getgid()],
        }
        if self.root_controlled_env_dir:
            for directory in self.backup_env.parents:
                owned[str(directory)] = [0, 0, 0o40755]
        return {
            "PATH": f"{self.bin}:{Path(PY).parent}:/usr/bin:/bin",
            "HOME": str(self.base),
            "PYTHONPATH": f"{self.site}:{REPO / 'src'}",
            "GATEA_STATE": str(self.state),
            "GATEA_ROOT": str(self.root),
            "GATEA_ROOT_OWNED": json.dumps(owned),
            "GATEA_FAKE_REPOCTL": json.dumps({
                "source": "a" * 64, "registry": "b" * 64, "recovery_point": recovery_point,
                "coverage": str(self.coverage),
            }),
        }

    def calls(self) -> list[str]:
        log = self.state / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def approve(self, phrase: str | None = None) -> tuple[Path, str]:
        plan, plan_sha = gate_a.load_plan(self.plan_path)
        text = self.base / "approval.txt"
        text.write_text(phrase if phrase is not None else gate_a.approval_phrase(plan, plan_sha) + "\n")
        out = self.base / "approvals"
        out.mkdir(exist_ok=True)
        return gate_a.record_approval(str(self.plan_path), str(text), str(out))

    def run_gate(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([PY, str(GATE), *args], env=self.env(), capture_output=True, text=True, timeout=60)

    def run_switch(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SWITCH), *args], env=self.env(), capture_output=True, text=True, timeout=60
        )


class FixtureCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = GateAFixture(Path(self._tmp.name))

    def tearDown(self) -> None:
        self.fx.health.stop()
        self._tmp.cleanup()

    def privileged_calls(self) -> list[str]:
        return [call for call in self.fx.calls() if not call.startswith("systemctl show")
                and not call.startswith("systemctl list-unit-files")]


class PlanSchemaTests(FixtureCase):
    def test_rejects_extra_fields_and_shell_text(self) -> None:
        cases = [
            {"notes": "run this"},
            {"unit": "repo-control.service; reboot"},
            {"managed_unit_source": "/tmp/$(id)"},
            {"installed_unit_path": "/etc/../tmp/x"},
            {"health_url": "http://example.com/healthz"},
            {"window_end_utc": utc(600)},
        ]
        for override in cases:
            with self.subTest(override=override):
                self.fx.write_plan(**override)
                with self.assertRaises(gate_a.GateAError):
                    gate_a.load_plan(self.fx.plan_path)

    def test_review_is_data_with_exact_approval_phrase(self) -> None:
        plan_sha = self.fx.write_plan()
        result = self.fx.run_gate("review", "--plan", str(self.fx.plan_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"GATE A APPROVED plan_sha256={plan_sha} window=", result.stdout)
        self.assertIn("latest switch-start deadline", result.stdout)
        self.assertIn("pre-write return before the agreed safe-stop", result.stdout)
        self.assertNotIn("#!", result.stdout)
        self.assertNotIn("```", result.stdout)
        self.assertNotIn("sudo", result.stdout)


class ManagedUnitContractTests(FixtureCase):
    def check(self) -> None:
        self.fx.write_plan()
        plan, _ = gate_a.load_plan(self.fx.plan_path)
        gate_a.check_managed_unit_contract(plan)

    def edit_unit(self, old: str, new: str) -> None:
        self.fx.managed_unit.write_text(self.fx.managed_unit.read_text().replace(old, new))

    def test_exact_runtime_and_namespace_contract_passes(self) -> None:
        self.check()

    def test_missing_scanner_directory_refuses_before_privilege_or_stop(self) -> None:
        self.fx.scanner.rmdir()
        self.fx.write_plan()
        result = self.fx.run_gate("preflight", "--plan", str(self.fx.plan_path))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("scanner cache directory does not exist", result.stderr)
        self.assertEqual([call for call in self.fx.calls() if call.startswith(("sudo", "install"))], [])
        self.assertFalse(any("systemctl stop" in call for call in self.fx.calls()))

    def test_missing_or_optional_readwrite_path_is_rejected(self) -> None:
        database = self.fx.candidate / "prepared" / "managed-prepared.sqlite3"
        for replacement, message in (
            (str(database).replace("managed-prepared.sqlite3", "absent.sqlite3"), "differ from the runtime contract"),
            (f"-{database}", "must not make required paths optional"),
        ):
            with self.subTest(replacement=replacement):
                original = self.fx.managed_unit.read_text()
                self.edit_unit(str(database), replacement)
                with self.assertRaisesRegex(gate_a.GateAError, message):
                    self.check()
                self.fx.managed_unit.write_text(original)

    def test_unbounded_restart_loop_is_rejected(self) -> None:
        self.edit_unit("StartLimitBurst=1", "StartLimitBurst=5")
        with self.assertRaisesRegex(gate_a.GateAError, "one attempt"):
            self.check()

    def test_scanner_directory_permissions_are_rejected(self) -> None:
        self.fx.scanner.chmod(0o755)
        with self.assertRaisesRegex(gate_a.GateAError, "mode 0700"):
            self.check()

    def test_transient_probe_uses_exact_sandbox_without_starting_repo_control(self) -> None:
        self.fx.write_plan()
        plan, _ = gate_a.load_plan(self.fx.plan_path)
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(gate_a.os, "geteuid", return_value=0), \
             mock.patch.object(gate_a.subprocess, "run", return_value=completed) as run:
            gate_a.sandbox_probe(plan)
        command = run.call_args.args[0]
        self.assertEqual(command[0], "systemd-run")
        self.assertIn(f"--property=ReadWritePaths={self.fx.candidate}/prepared/managed-prepared.sqlite3 "
                      f"{self.fx.candidate}/prepared/managed-prepared.sqlite3-wal "
                      f"{self.fx.candidate}/prepared/managed-prepared.sqlite3-shm "
                      f"{self.fx.candidate}/state/.locks {self.fx.scanner}", command)
        self.assertEqual(command[-1], "/usr/bin/true")
        self.assertNotIn("repoctl.cli", " ".join(command))

    def test_transient_probe_failure_is_a_stop(self) -> None:
        self.fx.write_plan()
        plan, _ = gate_a.load_plan(self.fx.plan_path)
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="226/NAMESPACE")
        with mock.patch.object(gate_a.os, "geteuid", return_value=0), \
             mock.patch.object(gate_a.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(gate_a.GateAError, "226/NAMESPACE"):
                gate_a.sandbox_probe(plan)


TRIPWIRES = (
    "sudo", "systemctl", "install", "cp", "mv", "ln", "tee", "curl", "ssh", "bash", "sh",
    "dbus-send", "busctl",
)
BUS_SOCKETS = ("/run/dbus/system_bus_socket", "/run/systemd/private")


@unittest.skipUnless(shutil.which("strace"), "strace is required for the review boundary trace")
class ReviewBoundaryTraceTests(FixtureCase):
    def trace(self, argv: list[str]) -> list[str]:
        trip = self.fx.base / "tripwires"
        trip.mkdir(exist_ok=True)
        fired = self.fx.base / "tripwire-fired"
        for name in TRIPWIRES:
            path = trip / name
            path.write_text(f"#!/bin/sh\necho {name} >> {fired}\nexit 1\n")
            path.chmod(0o755)
        out = self.fx.base / "trace.txt"
        env = {"PATH": f"{trip}:/usr/bin:/bin", "HOME": str(self.fx.base), "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run(
            ["strace", "-f", "-qq", "-e", "trace=execve,connect,openat", "-o", str(out), *argv],
            env=env, capture_output=True, text=True, timeout=60,
        )
        if "Operation not permitted" in result.stderr and "ptrace" in result.stderr:
            self.skipTest("ptrace is not permitted here")
        violations = []
        execs = [line for line in out.read_text().splitlines() if "execve(" in line and ") = 0" in line]
        violations += [f"exec: {line}" for line in execs[1:]]
        for line in out.read_text().splitlines():
            if "connect(" in line and (any(s in line for s in BUS_SOCKETS) or "AF_INET" in line):
                violations.append(f"connect: {line}")
            if "openat(" in line and any(flag in line for flag in ("O_WRONLY", "O_RDWR", "O_CREAT")):
                if '"/dev/' not in line:
                    violations.append(f"write-open: {line}")
        if fired.exists():
            violations.append(f"tripwire: {fired.read_text().strip()}")
        return violations

    def test_review_executes_nothing_and_cannot_reach_systemd(self) -> None:
        self.fx.write_plan()
        violations = self.trace([PY, str(GATE), "review", "--plan", str(self.fx.plan_path)])
        self.assertEqual(violations, [])

    def test_trace_detects_nested_helper_and_dbus_negative_controls(self) -> None:
        helper = self.fx.base / "helper.py"
        helper.write_text("import subprocess\ndef go():\n    subprocess.run(['sh', '-c', 'systemctl --version'])\n")
        nested = self.fx.base / "nested.py"
        nested.write_text(f"import sys\nsys.path.insert(0, {str(self.fx.base)!r})\nimport helper\nhelper.go()\n")
        dbus = self.fx.base / "dbus.py"
        dbus.write_text(textwrap.dedent("""
            import socket
            s = socket.socket(socket.AF_UNIX)
            try:
                s.connect("/run/dbus/system_bus_socket")
            except OSError:
                pass
            s.close()
        """))
        self.assertTrue(any(v.startswith("exec:") for v in self.trace([PY, str(nested)])))
        self.assertTrue(any(v.startswith("connect:") for v in self.trace([PY, str(dbus)])))


class SwitchGuardTests(FixtureCase):
    def assert_refused(self, result: subprocess.CompletedProcess) -> None:
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("No privileged step ran", result.stderr)
        self.assertEqual([c for c in self.fx.calls() if c.startswith(("sudo", "install"))], [])
        self.assertEqual(sha(self.fx.installed_unit), self.fx.legacy_sha)

    def test_refuses_without_exact_current_approval(self) -> None:
        self.fx.write_plan()
        record, digest = self.fx.approve()
        plan = str(self.fx.plan_path)
        self.assert_refused(self.fx.run_switch())
        self.assert_refused(self.fx.run_switch("--plan", plan, "--approval-record", str(record),
                                               "--approval-sha256", "0" * 64))
        tampered = self.fx.base / "tampered.json"
        data = json.loads(record.read_text())
        data["approval_text"] = data["approval_text"].replace("GATE A APPROVED", "GATE A REVIEWED")
        tampered.write_bytes(json.dumps(data).encode())
        self.assert_refused(self.fx.run_switch("--plan", plan, "--approval-record", str(tampered),
                                               "--approval-sha256", sha(tampered)))

    def test_refuses_for_other_plan_window_or_changed_scripts(self) -> None:
        self.fx.write_plan()
        record, digest = self.fx.approve()
        args = ["--plan", str(self.fx.plan_path), "--approval-record", str(record), "--approval-sha256", digest]
        variants = [
            {"readiness_timeout_seconds": 6},
            {"window_start_utc": utc(-120), "window_end_utc": utc(-60)},
            {"switch_script_sha256": "0" * 64},
            {"return_script_sha256": "0" * 64},
        ]
        for override in variants:
            with self.subTest(override=override):
                self.fx.write_plan(**override)
                self.assert_refused(self.fx.run_switch(*args))

    def test_approval_requires_exact_phrase(self) -> None:
        self.fx.write_plan()
        with self.assertRaises(gate_a.GateAError):
            self.fx.approve("Gate A approved, go ahead\n")


class PostAuthenticationTests(FixtureCase):
    def set_sudo_hooks(self, *, auth: str = "", install: str = "") -> None:
        auth = auth or "  :"
        install = install or "  :"
        stub = SUDO_STUB.replace(
            '[[ "$1" == "-v" ]] && exit 0',
            f'if [[ "$1" == "-v" ]]; then\n{auth}\n  exit 0\nfi',
        )
        stub = stub.replace("exec \"$@\"", f'if [[ "$1" == "install" ]]; then\n{install}\nfi\nexec "$@"')
        (self.fx.bin / "sudo").write_text(stub)

    def use_fixture_clock(self) -> None:
        path = self.fx.site / "sitecustomize.py"
        with path.open("a") as handle:
            handle.write(textwrap.dedent("""
                import datetime as _fixture_datetime
                _real_datetime = _fixture_datetime.datetime
                class _FixtureDateTime(_real_datetime):
                    @classmethod
                    def now(cls, tz=None):
                        value = super().now(tz)
                        if os.path.exists(os.path.join(os.environ["GATEA_STATE"], "clock-advanced")):
                            value += _fixture_datetime.timedelta(hours=2)
                        return value
                _fixture_datetime.datetime = _FixtureDateTime
            """))

    def switch(self) -> subprocess.CompletedProcess:
        self.fx.health.start()
        self.fx.write_plan()
        record, digest = self.fx.approve()
        return self.fx.run_switch("--plan", str(self.fx.plan_path), "--approval-record", str(record),
                                  "--approval-sha256", digest)

    def assert_no_stop(self) -> None:
        self.assertEqual(sha(self.fx.installed_unit), self.fx.legacy_sha)
        self.assertFalse(any(call.startswith("systemctl stop ") for call in self.fx.calls()))

    def assert_no_install(self) -> None:
        self.assertFalse(any(call.startswith("install ") for call in self.fx.calls()))

    def test_refuses_when_window_expires_during_authentication(self) -> None:
        self.use_fixture_clock()
        self.set_sudo_hooks(auth='  touch "$GATEA_STATE/clock-advanced"')
        result = self.switch()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("outside the plan window", result.stderr)
        self.assertIn("No privileged step ran", result.stderr)
        self.assert_no_stop()
        self.assert_no_install()

    def test_refuses_when_source_drifts_during_authentication(self) -> None:
        self.set_sudo_hooks(auth='  touch "$GATEA_STATE/source-stale"')
        result = self.switch()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("Source parity failed", result.stderr)
        self.assert_no_stop()
        self.assert_no_install()

    def test_refuses_when_recovery_expires_during_authentication(self) -> None:
        self.set_sudo_hooks(auth='  touch "$GATEA_STATE/recovery-stale"')
        result = self.switch()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("Fixture recovery point expired", result.stderr)
        self.assert_no_stop()
        self.assert_no_install()

    def test_refuses_when_held_wal_changes_during_authentication(self) -> None:
        self.set_sudo_hooks(auth=(
            '  printf "changed" > "$GATEA_ROOT/var/lib/repo-control-staging/'
            'm010.028-r9/prepared/managed-prepared.sqlite3-wal"'
        ))
        result = self.switch()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("COMMIT UNCERTAIN", result.stderr)
        self.assert_no_stop()
        self.assert_no_install()

    def test_refuses_when_approved_plan_changes_during_authentication(self) -> None:
        self.set_sudo_hooks(auth='  printf "\\\\n" >> "$GATEA_ROOT/../plan.json"')
        result = self.switch()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("approval record names a different plan", result.stderr)
        self.assert_no_stop()
        self.assert_no_install()

    def test_refuses_if_noninteractive_privilege_is_unavailable(self) -> None:
        stub = SUDO_STUB.replace('[[ "$1" == "-n" ]] && shift', '[[ "$1" == "-n" ]] && exit 1')
        (self.fx.bin / "sudo").write_text(stub)
        result = self.switch()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("managed environment install failed", result.stderr)
        self.assertIn("managed environment install was attempted", result.stderr)
        self.assert_no_stop()
        self.assert_no_install()

    def test_refuses_source_drift_after_managed_environment_install(self) -> None:
        self.set_sudo_hooks(install='  touch "$GATEA_STATE/source-stale"')
        result = self.switch()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("Source parity failed", result.stderr)
        self.assertTrue((self.fx.root / "etc" / "repo-control" / "managed.env").exists())
        self.assertNotIn("No privileged step ran", result.stderr)
        self.assertIn("managed environment install was attempted", result.stderr)
        self.assert_no_stop()

    def test_preflight_cannot_use_a_different_plan_from_the_approval(self) -> None:
        self.fx.health.start()
        self.fx.write_plan()
        record, digest = self.fx.approve()
        approved = self.fx.plan_path.read_bytes()
        (self.fx.state / "approved-plan.json").write_bytes(approved)
        other = json.loads(approved)
        other["source_inventory_sha256"] = "0" * 64
        (self.fx.state / "different-plan.json").write_text(json.dumps(other))
        self.set_sudo_hooks(install=(
            '  cp "$GATEA_STATE/different-plan.json" "$GATEA_ROOT/../plan.json"\n'
            '  touch "$GATEA_STATE/source-stale" "$GATEA_STATE/restore-approved-plan"'
        ))
        result = self.fx.run_switch("--plan", str(self.fx.plan_path), "--approval-record", str(record),
                                    "--approval-sha256", digest)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("approval record names a different plan", result.stderr)
        self.assert_no_stop()

    def test_refuses_recovery_expiry_during_final_publisher_checks(self) -> None:
        self.set_sudo_hooks(install='  touch "$GATEA_STATE/arm-recovery-after-publisher"')
        result = self.switch()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("Fixture recovery point expired", result.stderr)
        self.assertTrue((self.fx.root / "etc" / "repo-control" / "managed.env").exists())
        self.assert_no_stop()

    def test_refuses_window_expiry_during_final_preflight(self) -> None:
        self.use_fixture_clock()
        self.set_sudo_hooks(install='  touch "$GATEA_STATE/advance-clock-during-coverage"')
        result = self.switch()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("outside the plan window", result.stderr)
        self.assertTrue((self.fx.root / "etc" / "repo-control" / "managed.env").exists())
        self.assert_no_stop()


class ReadinessTests(FixtureCase):
    def readiness(self, expect: str = "managed", timeout: int = 5) -> str:
        self.fx.write_plan(readiness_timeout_seconds=timeout)
        plan, _ = gate_a.load_plan(self.fx.plan_path)
        state = {"ActiveState": "active", "FragmentPath": str(self.fx.installed_unit), "DropInPaths": ""}
        self.state = state
        with mock.patch.object(gate_a, "systemctl_show", lambda unit, props, *, timeout: dict(self.state)):
            return gate_a.readiness(plan, expect)

    def install_managed(self) -> None:
        shutil.copy(self.fx.managed_unit, self.fx.installed_unit)

    def test_waits_for_listener_then_verifies_exact_binding(self) -> None:
        self.install_managed()
        self.fx.health.start(delay=2.0)
        self.assertIn("READY sqlite_managed generation=" + "2" * 32, self.readiness())

    def test_accepts_health_response_slower_than_two_seconds(self) -> None:
        self.install_managed()
        self.fx.health.response_delay = 2.25
        self.fx.health.start()
        started = time.monotonic()
        self.assertIn("READY sqlite_managed generation=" + "2" * 32, self.readiness())
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 2.25)
        self.assertLess(elapsed, 5)

    def test_fails_on_wrong_authority_binding_unit_or_dropin(self) -> None:
        self.fx.health.start()
        with self.assertRaisesRegex(gate_a.GateAError, "installed unit is not the managed unit"):
            self.readiness()
        self.install_managed()
        self.fx.health.managed_mode = "legacy"
        with self.assertRaisesRegex(gate_a.GateAError, "exact planned managed binding"):
            self.readiness()
        self.fx.health.managed_mode = "exact"
        for field, value in (
            ("authority_generation", "9" * 32), ("database_identity", "9" * 64),
            ("selection_id", "9" * 32), ("selection_revision", 8), ("prompt_sha256", "9" * 64),
        ):
            with self.subTest(field=field):
                original = self.fx.binding[field]
                self.fx.binding[field] = value
                try:
                    self.fx.write_plan()
                    plan, _ = gate_a.load_plan(self.fx.plan_path)
                    self.fx.binding[field] = original
                    state = {"ActiveState": "active", "FragmentPath": str(self.fx.installed_unit), "DropInPaths": ""}
                    with mock.patch.object(gate_a, "systemctl_show", lambda unit, props, *, timeout: state):
                        with self.assertRaisesRegex(gate_a.GateAError, "exact planned managed binding"):
                            gate_a.readiness(plan, "managed")
                finally:
                    self.fx.binding[field] = original
        self.fx.write_plan()
        plan, _ = gate_a.load_plan(self.fx.plan_path)
        dropin = {"ActiveState": "active", "FragmentPath": str(self.fx.installed_unit), "DropInPaths": "/x.conf"}
        with mock.patch.object(gate_a, "systemctl_show", lambda unit, props, *, timeout: dropin):
            with self.assertRaisesRegex(gate_a.GateAError, "drop-ins"):
                gate_a.readiness(plan, "managed")
        shutil.copy(self.fx.saved_legacy, self.fx.installed_unit)
        self.assertEqual(self.readiness("legacy"), "READY legacy_files with null managed binding")

    def test_never_listening_times_out_within_bound(self) -> None:
        self.install_managed()
        started = time.monotonic()
        with self.assertRaisesRegex(gate_a.GateAError, "timed out"):
            self.readiness(timeout=5)
        self.assertLess(time.monotonic() - started, 5)

    def test_does_not_accept_a_redirected_health_response(self) -> None:
        self.install_managed()
        self.fx.health.redirect_health = True
        self.fx.health.start()
        started = time.monotonic()
        with self.assertRaisesRegex(gate_a.GateAError, "timed out"):
            self.readiness(timeout=5)
        self.assertLess(time.monotonic() - started, 5)

    def test_slow_stream_cannot_extend_deadline(self) -> None:
        self.install_managed()
        self.fx.health.stream_interval = 0.2
        self.fx.health.start()
        started = time.monotonic()
        with self.assertRaisesRegex(gate_a.GateAError, "timed out"):
            self.readiness(timeout=5)
        self.assertLess(time.monotonic() - started, 5)

    def test_systemctl_cannot_extend_deadline(self) -> None:
        self.install_managed()
        self.fx.write_plan(readiness_timeout_seconds=5)
        plan, _ = gate_a.load_plan(self.fx.plan_path)
        systemctl = self.fx.bin / "systemctl"
        systemctl.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(6)\n")
        started = time.monotonic()
        with mock.patch.dict(os.environ, {"PATH": f"{self.fx.bin}:/usr/bin:/bin"}):
            with self.assertRaisesRegex(gate_a.GateAError, "timed out"):
                gate_a.readiness(plan, "managed")
        self.assertLess(time.monotonic() - started, 5)


class PublisherBindingTests(FixtureCase):
    def check(self) -> subprocess.CompletedProcess:
        self.fx.write_plan()
        return self.fx.run_gate("publisher-binding", "--plan", str(self.fx.plan_path))

    def test_passes_only_for_this_candidate(self) -> None:
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS publishers bind only to this candidate", result.stdout)

    def test_rejects_other_candidate_paths_and_running_publishers(self) -> None:
        other = "/var/lib/repo-control-staging/m010.028-r3/prepared/managed-prepared.sqlite3"
        self.fx.set_publisher(ReadOnlyPaths=other)
        self.assertIn("references another candidate", self.check().stderr)
        self.fx.write_systemctl()
        self.fx.set_publisher(ReadWritePaths="/home/chuck/elsewhere")
        self.assertIn("may write outside this candidate", self.check().stderr)
        self.fx.write_systemctl(other_timer_state="enabled")
        self.assertIn("Another backup publisher", self.check().stderr)
        self.fx.write_systemctl()
        self.fx.backup_env_source.write_text(self.fx.backup_env_source.read_text().replace("m010.028-r9", "m010.028-r3"))
        self.assertNotEqual(self.check().returncode, 0)


class OperatorShellTests(FixtureCase):
    def interactive(self, script: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "--norc", "--noprofile", "-i"], input=script, env=self.fx.env(),
            capture_output=True, text=True, timeout=90,
        )

    def approved_args(self) -> str:
        self.fx.write_plan()
        record, digest = self.fx.approve()
        return f"--plan {self.fx.plan_path} --approval-record {record} --approval-sha256 {digest}"

    def test_successful_switch_reports_exact_binding(self) -> None:
        self.fx.health.start()
        args = self.approved_args().split()
        result = self.fx.run_switch(*args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("GATE A SWITCH VERIFIED", result.stdout)
        self.assertEqual(sha(self.fx.installed_unit), sha(self.fx.managed_unit))
        order = [c.split()[2:4] for c in self.privileged_calls() if c.startswith("sudo -n ")]
        self.assertEqual(order[1:], [
            ["systemctl", "stop"], ["install", "-o"], ["systemctl", "daemon-reload"], ["systemctl", "start"],
        ])

    def test_failed_switch_does_not_close_errexit_operator_shell(self) -> None:
        self.fx.health.managed_mode = "never"
        self.fx.health.start()
        args = self.approved_args()
        result = self.interactive(
            "set -euo pipefail\n"
            f'if bash {SWITCH} {args}; then echo "GATE A: PASS"; else echo "GATE A: STOPPED (exit $?)"; fi\n'
            "echo SHELL_ALIVE\n"
            f"source {SWITCH} || true\n"
            "echo SOURCE_GUARD_ALIVE\n"
        )
        self.assertIn("GATE A: STOPPED (exit 3)", result.stdout)
        self.assertIn("SHELL_ALIVE", result.stdout)
        self.assertIn("SOURCE_GUARD_ALIVE", result.stdout)
        self.assertIn(f"bash {RETURN} --plan {self.fx.plan_path}", result.stderr)

    def test_bare_failure_under_errexit_closes_shell(self) -> None:
        # Negative control for the suspected (unproven) incident mechanism.
        result = self.interactive(
            "set -euo pipefail\n"
            "curl --fail --silent http://127.0.0.1:9/healthz\n"
            "echo SHELL_ALIVE\n"
        )
        self.assertNotIn("SHELL_ALIVE", result.stdout)


@unittest.skipUnless(Path("/usr/sbin/sshd").exists(), "sshd is required for the fresh-session test")
class FreshSshReturnTests(FixtureCase):
    def start_sshd(self) -> int:
        ssh_dir = self.fx.base / "ssh"
        ssh_dir.mkdir()
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(ssh_dir / "host")], check=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(ssh_dir / "client")], check=True)
        shutil.copy(ssh_dir / "client.pub", ssh_dir / "authorized_keys")
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        config = ssh_dir / "sshd_config"
        config.write_text(textwrap.dedent(f"""
            ListenAddress 127.0.0.1
            Port {port}
            HostKey {ssh_dir / 'host'}
            AuthorizedKeysFile {ssh_dir / 'authorized_keys'}
            PidFile {ssh_dir / 'sshd.pid'}
            UsePAM no
            StrictModes no
            PasswordAuthentication no
            KbdInteractiveAuthentication no
            AllowUsers {os.environ.get('USER', 'chuck')}
        """))
        self.sshd = subprocess.Popen(
            ["/usr/sbin/sshd", "-D", "-e", "-f", str(config)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        def stop_sshd() -> None:
            self.sshd.kill()
            self.sshd.wait(timeout=10)

        self.addCleanup(stop_sshd)
        self.ssh_dir = ssh_dir
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                return port
            except OSError:
                time.sleep(0.1)
        self.skipTest("temporary sshd did not start")
        return port

    def ssh(self, port: int, command: str) -> subprocess.CompletedProcess:
        exports = " ".join(f"{key}={shlex.quote(value)}" for key, value in self.fx.env().items())
        return subprocess.run(
            ["ssh", "-T", "-p", str(port), "-i", str(self.ssh_dir / "client"),
             "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
             "-o", f"UserKnownHostsFile={self.ssh_dir / 'known_hosts'}", "127.0.0.1",
             f"env -i {exports} bash --norc --noprofile -c {shlex.quote(command)}"],
            capture_output=True, text=True, timeout=90,
        )

    def test_return_verifies_from_fresh_session_after_failed_switch(self) -> None:
        port = self.start_sshd()
        self.fx.health.managed_mode = "never"
        self.fx.health.start()
        self.fx.write_plan()
        record, digest = self.fx.approve()
        first = self.ssh(port, (
            "set -euo pipefail; "
            f"if bash {SWITCH} --plan {self.fx.plan_path} --approval-record {record} --approval-sha256 {digest}; "
            'then echo "GATE A: PASS"; else echo "GATE A: STOPPED (exit $?)"; fi'
        ))
        if first.returncode == 255:
            self.skipTest(f"temporary sshd login unavailable: {first.stderr.strip()}")
        self.assertIn("GATE A: STOPPED (exit 3)", first.stdout, first.stderr)
        self.assertEqual(sha(self.fx.installed_unit), sha(self.fx.managed_unit))

        second = self.ssh(port, (
            f'if bash {RETURN} --plan {self.fx.plan_path}; then echo "RETURN: PASS"; '
            'else echo "RETURN: FAILED (exit $?)"; fi'
        ))
        self.assertIn("RETURN VERIFIED", second.stdout, second.stderr)
        self.assertIn("RETURN: PASS", second.stdout)
        self.assertEqual(sha(self.fx.installed_unit), self.fx.legacy_sha)

    def test_return_refuses_when_held_paths_changed(self) -> None:
        self.fx.write_plan()
        (self.fx.candidate / "prepared" / "managed-prepared.sqlite3-wal").write_bytes(b"committed")
        result = subprocess.run(["bash", str(RETURN), "--plan", str(self.fx.plan_path)],
                                env=self.fx.env(), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 2)
        self.assertIn("COMMIT UNCERTAIN", result.stderr)
        self.assertIn("forward recovery", result.stderr)
        self.assertEqual([c for c in self.fx.calls() if c.startswith("sudo")], [])


class HeldClassificationTests(FixtureCase):
    def held(self) -> subprocess.CompletedProcess:
        self.fx.write_plan()
        return self.fx.run_gate("held-check", "--plan", str(self.fx.plan_path))

    def test_shm_content_and_lock_activity_are_expected(self) -> None:
        self.assertIn("PASS UNCHANGED", self.held().stdout)
        (self.fx.candidate / "prepared" / "managed-prepared.sqlite3-shm").write_bytes(b"reader marks")
        self.fx.lock.write_bytes(b"")
        os.utime(self.fx.lock)
        result = self.held()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("EXPECTED_ACTIVITY WAL-index (-shm) content changed", result.stdout)

    def test_return_proceeds_after_expected_shm_activity(self) -> None:
        self.fx.health.start()
        self.fx.write_plan()
        shutil.copy(self.fx.managed_unit, self.fx.installed_unit)
        (self.fx.candidate / "prepared" / "managed-prepared.sqlite3-shm").write_bytes(b"reader marks")
        result = subprocess.run(["bash", str(RETURN), "--plan", str(self.fx.plan_path)],
                                env=self.fx.env(), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("EXPECTED_ACTIVITY", result.stdout)
        self.assertIn("RETURN VERIFIED", result.stdout)

    def test_database_wal_or_metadata_changes_are_commit_uncertain(self) -> None:
        prepared = self.fx.candidate / "prepared"
        shm = prepared / "managed-prepared.sqlite3-shm"
        def recreate(path: Path) -> None:
            replacement = path.with_name(path.name + ".new")
            replacement.write_bytes(path.read_bytes())
            replacement.chmod(path.stat().st_mode & 0o777)
            os.replace(replacement, path)

        mutations = {
            "database": lambda: (prepared / "managed-prepared.sqlite3").write_bytes(b"page"),
            "wal": lambda: (prepared / "managed-prepared.sqlite3-wal").write_bytes(b"frame"),
            "rollback journal": lambda: (prepared / "managed-prepared.sqlite3-journal").write_bytes(b"j"),
            "shm mode": lambda: shm.chmod(0o600),
            "shm recreated": lambda: recreate(shm),
            "lock recreated": lambda: recreate(self.fx.lock),
            "unreadable database": lambda: (prepared / "managed-prepared.sqlite3").chmod(0),
        }
        for label, mutate in mutations.items():
            with self.subTest(label):
                self.tearDown()
                self.setUp()
                prepared = self.fx.candidate / "prepared"
                shm = prepared / "managed-prepared.sqlite3-shm"
                mutate()
                result = self.held()
                self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
                self.assertIn("COMMIT UNCERTAIN", result.stderr)

    def test_switch_does_not_offer_return_when_commit_uncertain(self) -> None:
        self.fx.health.on_managed = self.fx.candidate / "prepared" / "managed-prepared.sqlite3-wal"
        self.fx.health.start()
        self.fx.write_plan()
        record, digest = self.fx.approve()
        result = self.fx.run_switch("--plan", str(self.fx.plan_path), "--approval-record", str(record),
                                    "--approval-sha256", digest)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn("Do not return to legacy", result.stderr)
        self.assertNotIn(f"bash {RETURN}", result.stderr)


class BackupEnvironmentTests(FixtureCase):
    def check(self) -> subprocess.CompletedProcess:
        self.fx.write_plan()
        shutil.copy(self.fx.backup_env_source, self.fx.backup_env)
        self.fx.backup_env.chmod(0o640)
        return self.fx.run_gate("publisher-binding", "--plan", str(self.fx.plan_path))

    def edit_source(self, old: str, new: str) -> None:
        text = self.fx.backup_env_source.read_text()
        self.fx.backup_env_source.write_text(text.replace(old, new) if old else text + new)

    def test_rejects_secrets_unknown_or_missing_bindings(self) -> None:
        cases = {
            "unknown key": ("", "REPOCTL_BACKUP_PASSWORD=hunter2\n"),
            "credential in mount source": ("//192.168.1.171/", "//user:pw@192.168.1.171/"),
            "missing key": ("REPOCTL_BACKUP_MAX_AGE_SECONDS=1200\n", ""),
            "repeated key": ("", "REPOCTL_BACKUP_MAX_AGE_SECONDS=1200\n"),
        }
        original = self.fx.backup_env_source.read_text()
        for label, (old, new) in cases.items():
            with self.subTest(label):
                self.fx.backup_env_source.write_text(original)
                self.edit_source(old, new)
                result = self.check()
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("backup environment", result.stderr)
                self.assertNotIn("hunter2", result.stderr)

    def test_requires_root_controlled_directory_and_candidate_specific_path(self) -> None:
        self.assertEqual(self.check().returncode, 0)
        self.fx.root_controlled_env_dir = False
        self.assertIn("not a root-controlled directory", self.check().stderr)
        self.fx.root_controlled_env_dir = True
        plan = self.fx.plan()
        plan["publisher"]["backup_env_path"] = str(self.fx.backup_env.with_name("backup-m010028.env"))
        self.fx.plan_path.write_text(json.dumps(plan))
        result = self.fx.run_gate("validate", "--plan", str(self.fx.plan_path))
        self.assertIn("candidate-specific file backup-m010028-r9.env", result.stderr)

    def test_r4_example_contains_only_approved_candidate_bindings(self) -> None:
        source = REPO / "ops" / "backup.m010028-r4.env.example"
        values = gate_a._backup_environment({"publisher": {"backup_env_source": str(source)}})
        self.assertEqual(set(values), set(gate_a.BACKUP_ENV_RULES))
        self.assertEqual(
            values["REPOCTL_BACKUP_SOURCE"],
            "/var/lib/repo-control-staging/m010.028-r4/prepared/managed-prepared.sqlite3",
        )
        self.assertEqual(
            values["REPOCTL_BACKUP_COVERAGE_PATH"],
            "/home/chuck/.local/state/repo-control-backup/staging/m010.028-r4/"
            ".repoctl-verified-recovery-point.json",
        )
        self.assertFalse(any("R3" in value or "PASSWORD" in value or "CREDENTIAL" in value
                             for value in values.values()))

    def test_r8_proposals_are_candidate_specific_and_bounded(self) -> None:
        source = REPO / "ops" / "backup.m010028-r8.env.example"
        values = gate_a._backup_environment({"publisher": {"backup_env_source": str(source)}})
        self.assertEqual(set(values), set(gate_a.BACKUP_ENV_RULES))
        self.assertTrue(all("m010.028-r7" not in value for value in values.values()))
        self.assertEqual(values["REPOCTL_BACKUP_MAX_AGE_SECONDS"], "1200")
        unit = (REPO / "ops" / "systemd" / "repo-control-m010028-r8-managed.service").read_text()
        self.assertIn("StartLimitBurst=1", unit)
        self.assertEqual(unit.count("/home/chuck/.cache/repoctl/m010.028-r8/scanner"), 2)
        self.assertNotIn("m010.028-r7", unit)


if __name__ == "__main__":
    unittest.main()
