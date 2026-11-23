from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid


_REPOSITORY_ROOT = Path(__file__).parents[1]
_INSTALLER = _REPOSITORY_ROOT / "ops/m011001/install-procedure-executor.sh"
_SOURCES = (
    "repo-control-m011001-procedure-store.conf",
    "repo-control-procedure-update@.service",
    "repo-control-procedure-update@repo-control-ii.service.d/m011001-write-surface.conf",
    "repo-control-procedure-update@vocab-app.service.d/m011001-write-surface.conf",
    "repo-control-procedure-update@photo-organizer.service.d/m011001-write-surface.conf",
)
_TARGETS = (
    "repo-control.service.d/m011001-procedure-store.conf",
    "repo-control-procedure-update@.service",
    "repo-control-procedure-update@repo-control-ii.service.d/m011001-write-surface.conf",
    "repo-control-procedure-update@vocab-app.service.d/m011001-write-surface.conf",
    "repo-control-procedure-update@photo-organizer.service.d/m011001-write-surface.conf",
)


class ProcedureExecutorInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="repoctl-procedure-installer.", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o700)
        self.source = self.root / "source"
        self.source.mkdir()
        for relative in _SOURCES:
            target = self.source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_REPOSITORY_ROOT / "ops/systemd" / relative, target)
        self.transactions = self.root / "transactions"
        self.transactions.mkdir(mode=0o700)
        self.systemd = self.root / "systemd"
        self.backup = self.root / "backup"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.systemctl_log = self.root / "systemctl.log"
        self._link_tools()
        self._write_systemctl()
        self._write_curl()
        self._write_installer()
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "PATH": str(self.bin),
                "REPOCTL_PROCEDURE_INSTALLER_FIXTURE_ROOT": str(self.root),
                "SYSTEMCTL_LOG": str(self.systemctl_log),
                "TRANSACTIONS_PATH": str(self.transactions),
                "HEALTH_JSON": json.dumps(
                    {
                        "information_store_binding": {
                            "authority": "sqlite_managed",
                            "authority_state_root": str(self.root / "state"),
                            "authority_generation": "0" * 32,
                            "database_identity": "0" * 64,
                        }
                    }
                ),
            }
        )

    def _link_tools(self) -> None:
        for name in (
            "sha256sum",
            "stat",
            "cmp",
            "install",
            "cp",
            "touch",
            "mkdir",
            "rmdir",
            "mv",
            "dirname",
            "date",
            "id",
            "sleep",
        ):
            executable = shutil.which(name)
            self.assertIsNotNone(executable, f"Required test utility is unavailable: {name}")
            (self.bin / name).symlink_to(executable)
        systemd_analyze = shutil.which("systemd-analyze")
        self.assertIsNotNone(systemd_analyze)
        self._write_executable(
            self.bin / "systemd-analyze",
            "#!/bin/sh\n"
            'if [ "${ANALYZE_RESULT:-0}" -ne 0 ]; then exit "${ANALYZE_RESULT}"; fi\n'
            f'exec "{systemd_analyze}" "$@"\n',
        )

    def _write_executable(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def _write_systemctl(self) -> None:
        self._write_executable(
            self.bin / "systemctl",
            """#!/bin/sh
printf '%s\\n' "$*" >>"$SYSTEMCTL_LOG"
case "$1" in
  is-active|is-enabled)
    case "$3" in
      repo-control.service|repo-control-backup-m010028-r8.timer|repo-control-backup-monitor-m010028-r8.timer) exit 0 ;;
      repo-control-procedure-update@*.service) exit 1 ;;
      *) exit 1 ;;
    esac
    ;;
  show)
    case " $* " in
      *" Environment "*) printf 'REPOCTL_PROCEDURE_TRANSACTION_ROOT=%s\\n' "$TRANSACTIONS_PATH" ;;
      *" ReadWritePaths "*) printf '%s\\n' "$TRANSACTIONS_PATH" ;;
      *) exit 1 ;;
    esac
    ;;
  cat) printf '[Service]\\n' ;;
  daemon-reload|restart|status) exit 0 ;;
  *) exit 1 ;;
esac
""",
        )

    def _write_curl(self) -> None:
        self._write_executable(
            self.bin / "curl",
            """#!/bin/sh
case " $* " in
  *"/healthz "*) printf '%s\\n' "$HEALTH_JSON" ;;
  *"/procedure-updates "*) printf 'repo-control-ii vocab-app photo-organizer\\n' ;;
  *) exit 1 ;;
esac
""",
        )

    def _write_installer(self) -> None:
        real_install = shutil.which("install")
        (self.bin / "install").unlink()
        self._write_executable(
            self.bin / "install",
            """#!/bin/sh
last=
for arg do last=$arg; done
if [ -n "${FAIL_INSTALL_TARGET:-}" ] && [ "$last" = "$FAIL_INSTALL_TARGET" ]; then
  echo 'injected fixture copy failure' >&2
  exit 7
fi
exec REAL_INSTALL "$@"
""".replace("REAL_INSTALL", f'"{real_install}"'),
        )

    def _run(self, action: str, *, installer: Path | None = None, extra: dict | None = None):
        environment = dict(self.environment)
        if extra:
            environment.update(extra)
        return subprocess.run(
            ["/bin/bash", str(installer or _INSTALLER), action],
            cwd=_REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def _snapshot(self, root: Path) -> dict[str, bytes | None] | None:
        if not root.exists():
            return None
        return {
            path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
            for path in sorted(root.rglob("*"))
        }

    def test_missing_systemd_analyze_fails_before_files_or_service_changes(self) -> None:
        (self.bin / "systemd-analyze").unlink()
        before_source = self._snapshot(self.source)
        result = self._run("install")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("systemd-analyze", result.stderr)
        self.assertEqual(self._snapshot(self.source), before_source)
        self.assertIsNone(self._snapshot(self.systemd))
        self.assertIsNone(self._snapshot(self.backup))
        self.assertFalse(self.systemctl_log.exists())

    def test_nonzero_verifier_fails_before_files_or_service_changes(self) -> None:
        before_source = self._snapshot(self.source)
        result = self._run("install", extra={"ANALYZE_RESULT": "7"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._snapshot(self.source), before_source)
        self.assertIsNone(self._snapshot(self.systemd))
        self.assertIsNone(self._snapshot(self.backup))
        self.assertFalse(self.systemctl_log.exists())

    def test_invalid_candidate_unit_is_rejected_by_real_systemd_analyze(self) -> None:
        installer = self.root / "installer-fixture.sh"
        source_unit = self.source / "repo-control-procedure-update@.service"
        invalid_unit = b"[Unit]\nDescription=Invalid fixture\n[Service]\nType=oneshot\n"
        source_unit.write_bytes(invalid_unit)
        installer_text = _INSTALLER.read_text(encoding="utf-8")
        expected = hashlib.sha256(
            (_REPOSITORY_ROOT / "ops/systemd/repo-control-procedure-update@.service").read_bytes()
        ).hexdigest()
        installer.write_text(
            installer_text.replace(expected, hashlib.sha256(invalid_unit).hexdigest(), 1),
            encoding="utf-8",
        )
        hostile_bin = self.root / "hostile-bin"
        hostile_bin.mkdir(mode=0o700)
        hostile_marker = self.root / "hostile-systemd-analyze-was-used"
        self._write_executable(
            hostile_bin / "systemd-analyze",
            f"#!/bin/sh\nprintf hostile >{hostile_marker}\nexit 0\n",
        )
        result = self._run(
            "install",
            installer=installer,
            extra={"PATH": f"{hostile_bin}:{self.environment['PATH']}"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(hostile_marker.exists())
        self.assertIsNone(self._snapshot(self.systemd))
        self.assertIsNone(self._snapshot(self.backup))
        self.assertFalse(self.systemctl_log.exists())

    def test_successful_preflight_is_read_only(self) -> None:
        result = self._run("preflight")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PREFLIGHT PASS", result.stdout)
        self.assertIsNone(self._snapshot(self.systemd))
        self.assertIsNone(self._snapshot(self.backup))
        calls = self.systemctl_log.read_text(encoding="utf-8").splitlines()
        self.assertFalse(any(call.startswith(("daemon-reload", "restart ")) for call in calls))

    def test_effective_transient_service_enforces_candidate_write_surface(self) -> None:
        systemd_run = shutil.which("systemd-run")
        self.assertIsNotNone(systemd_run)
        repository = self.root / "sandbox-repository"
        transaction_root = self.root / "sandbox-transactions"
        denied_root = self.root / "sandbox-denied"
        for directory in (
            repository / ".git",
            repository / "docs/context",
            repository / "docs/milestones",
            transaction_root,
            denied_root,
        ):
            directory.mkdir(parents=True, mode=0o700)

        script = (
            "from pathlib import Path\n"
            "import os, sys\n"
            "repository, transactions, denied = map(Path, sys.argv[1:4])\n"
            "assert os.geteuid() == int(sys.argv[4])\n"
            "assert os.getegid() == int(sys.argv[5])\n"
            "allowed = repository / 'generated-root.txt'\n"
            "allowed.write_text('fixture', encoding='utf-8')\n"
            "assert allowed.stat().st_mode & 0o777 == 0o600\n"
            "(transactions / 'journal-probe').write_text('fixture', encoding='utf-8')\n"
            "denied_paths, writable_paths = [], []\n"
            "for path in (repository / '.git/probe', repository / 'docs/context/probe', "
            "repository / 'docs/milestones/probe', denied / 'probe'):\n"
            "    try:\n"
            "        path.write_text('forbidden', encoding='utf-8')\n"
            "    except OSError:\n"
            "        denied_paths.append(str(path))\n"
            "        continue\n"
            "    writable_paths.append(str(path))\n"
            "status = Path('/proc/self/status').read_text(encoding='utf-8')\n"
            "print('SANDBOX_REPORT=' + __import__('json').dumps({\n"
            "    'uid': os.geteuid(), 'gid': os.getegid(),\n"
            "    'no_new_privileges': 'NoNewPrivs:\\t1' in status,\n"
            "    'denied_paths': denied_paths, 'writable_paths': writable_paths,\n"
            "}))\n"
        )
        command = [
            systemd_run,
            "--user",
            "--wait",
            "--collect",
            "--pipe",
            f"--unit=repoctl-installer-sandbox-{uuid.uuid4().hex}",
            f"--working-directory={repository}",
            "--property=ProtectSystem=strict",
            "--property=ProtectHome=read-only",
            "--property=NoNewPrivileges=yes",
            "--property=UMask=0077",
            f"--property=ReadWritePaths={repository} {transaction_root}",
            f"--property=ReadOnlyPaths={repository / '.git'} "
            f"{repository / 'docs/context'} {repository / 'docs/milestones'}",
            sys.executable,
            "-c",
            script,
            str(repository),
            str(transaction_root),
            str(denied_root),
            str(os.getuid()),
            str(os.getgid()),
        ]
        result = subprocess.run(
            command,
            cwd=_REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report_line = next(
            (line.removeprefix("SANDBOX_REPORT=") for line in result.stdout.splitlines()
             if line.startswith("SANDBOX_REPORT=")),
            None,
        )
        self.assertIsNotNone(report_line, result.stdout)
        report = json.loads(report_line)
        self.assertEqual((report["uid"], report["gid"]), (os.getuid(), os.getgid()))
        self.assertTrue(report["no_new_privileges"])
        self.assertEqual((repository / "generated-root.txt").read_text(encoding="utf-8"), "fixture")
        self.assertEqual((transaction_root / "journal-probe").read_text(encoding="utf-8"), "fixture")
        if report["writable_paths"]:
            self.skipTest(
                "The unprivileged systemd user manager did not enforce candidate read-only paths; "
                "system-service sandbox enforcement remains unverified."
            )
        self.assertEqual(len(report["denied_paths"]), 4)
        self.assertFalse(list((repository / ".git").iterdir()))
        self.assertFalse(list((repository / "docs/context").iterdir()))
        self.assertFalse(list((repository / "docs/milestones").iterdir()))
        self.assertFalse(list(denied_root.iterdir()))

    def test_candidate_service_instance_and_dropin_verify_in_disposable_systemd_root(self) -> None:
        systemd_analyze = shutil.which("systemd-analyze")
        self.assertIsNotNone(systemd_analyze)
        candidate_root = self.root / "candidate-system-root"
        systemd_directory = candidate_root / "etc/systemd/system"
        template = systemd_directory / "repo-control-procedure-update@.service"
        template.parent.mkdir(parents=True)
        shutil.copy2(self.source / _SOURCES[1], template)
        instance_dropin = (
            systemd_directory
            / "repo-control-procedure-update@photo-organizer.service.d/m011001-write-surface.conf"
        )
        instance_dropin.parent.mkdir(parents=True)
        shutil.copy2(self.source / _SOURCES[4], instance_dropin)
        (systemd_directory / "repo-control.service").write_text(
            "[Unit]\nDescription=Disposable Repo Control dependency\n"
            "[Service]\nType=oneshot\nExecStart=/usr/bin/true\n",
            encoding="utf-8",
        )
        (systemd_directory / "sysinit.target").write_text(
            "[Unit]\nDescription=Disposable system initialization target\n",
            encoding="utf-8",
        )
        python = (
            candidate_root
            / "home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python"
        )
        python.parent.mkdir(parents=True)
        shutil.copy2("/usr/bin/true", python)
        python.chmod(0o755)
        (candidate_root / "home/chuck/projects/repo-control-ii").mkdir(parents=True)
        (candidate_root / "home/chuck/projects/photo-organizer-dev/docs/context").mkdir(parents=True)
        (candidate_root / "home/chuck/projects/photo-organizer-dev/docs/milestones").mkdir(parents=True)

        verified = subprocess.run(
            [
                systemd_analyze,
                f"--root={candidate_root}",
                "verify",
                "repo-control-procedure-update@photo-organizer.service",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        effective_instance = subprocess.run(
            [
                systemd_analyze,
                f"--root={candidate_root}",
                "cat-config",
                "systemd/system/repo-control-procedure-update@photo-organizer.service",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(effective_instance.returncode, 0, effective_instance.stderr)
        self.assertIn("ReadWritePaths=/home/chuck/projects/photo-organizer-dev", effective_instance.stdout)
        self.assertIn("ReadOnlyPaths=/home/chuck/projects/photo-organizer-dev/.git", effective_instance.stdout)
        self.assertIn("ReadOnlyPaths=/home/chuck/projects/photo-organizer-dev/docs/context", effective_instance.stdout)
        self.assertIn("ReadOnlyPaths=/home/chuck/projects/photo-organizer-dev/docs/milestones", effective_instance.stdout)

    def test_install_and_repeat_run_verify_exact_files_without_restarting_twice(self) -> None:
        first = self._run("install")
        self.assertEqual(first.returncode, 0, first.stderr)
        for relative, target in zip(_SOURCES, _TARGETS, strict=True):
            installed = self.systemd / target
            self.assertEqual(installed.read_bytes(), (self.source / relative).read_bytes())
            self.assertEqual(installed.stat().st_mode & 0o777, 0o644)

        second = self._run("install")
        self.assertEqual(second.returncode, 0, second.stderr)
        calls = self.systemctl_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(sum(call == "restart repo-control.service" for call in calls), 1)
        self.assertEqual(sum(call == "daemon-reload" for call in calls), 2)

    def test_partial_copy_failure_is_restorable_by_the_existing_undo_action(self) -> None:
        failed_target = self.systemd / _TARGETS[1]
        failed = self._run("install", extra={"FAIL_INSTALL_TARGET": str(failed_target)})
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(
            (self.systemd / _TARGETS[0]).read_bytes(),
            (self.source / _SOURCES[0]).read_bytes(),
        )
        calls_before_undo = self.systemctl_log.read_text(encoding="utf-8").splitlines()
        self.assertFalse(any(call.startswith(("daemon-reload", "restart ")) for call in calls_before_undo))

        restored = self._run("undo")
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertFalse((self.systemd / _TARGETS[0]).exists())
        self.assertTrue((self.backup / "capture-complete").is_file())
        calls_after_undo = self.systemctl_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(sum(call == "restart repo-control.service" for call in calls_after_undo), 1)


if __name__ == "__main__":
    unittest.main()
