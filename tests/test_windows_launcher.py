from pathlib import Path
import unittest


PACKAGE = Path(__file__).resolve().parents[1] / "ops" / "windows" / "RepoControlLauncher"


class WindowsLauncherStaticTests(unittest.TestCase):
    """Static artifact checks; Windows runtime acceptance is separate."""

    def test_hidden_ssh_keeps_security_and_bounded_failure_options(self) -> None:
        script = (PACKAGE / "open-repo-control.ps1").read_text(encoding="utf-8")
        for option in (
            "-WindowStyle Hidden -RedirectStandardError $SshErrorLogPath -PassThru",
            "ExitOnForwardFailure=yes",
            "BatchMode=yes",
            "ConnectTimeout=10",
            "ServerAliveInterval=$SshKeepAliveIntervalSeconds",
            "ServerAliveCountMax=$SshKeepAliveCountMax",
            "Stop-Process -Id $sshProcess.Id",
            "Write-Host \"To stop this tunnel: Stop-Process -Id $($sshProcess.Id)\"",
        ):
            self.assertIn(option, script)
        self.assertNotIn("StrictHostKeyChecking=no", script)
        self.assertNotIn("UserKnownHostsFile=", script)

    def test_hidden_task_uses_tunnel_only_and_limited_interactive_user(self) -> None:
        script = (PACKAGE / "install-startup-task.ps1").read_text(encoding="utf-8")
        self.assertIn('-Execute $ScriptHostPath', script)
        self.assertIn('//B //Nologo `"$LauncherPath`"', script)
        self.assertIn('"start-tunnel-hidden.vbs"', script)
        self.assertIn("-LogonType Interactive", script)
        self.assertIn("-RunLevel Limited", script)

    def test_logon_wrapper_hides_process_at_creation_and_preserves_exit_code(self) -> None:
        script = (PACKAGE / "start-tunnel-hidden.vbs").read_text(encoding="utf-8")
        self.assertIn("WScript.ScriptFullName", script)
        self.assertIn('"open-repo-control.ps1"', script)
        self.assertIn("-NoProfile -WindowStyle Hidden", script)
        self.assertIn('-TunnelOnly"', script)
        self.assertIn("result = shell.Run(command, 0, True)", script)
        self.assertIn("WScript.Quit result", script)

    def test_failures_have_diagnostics_and_nonzero_exit(self) -> None:
        script = (PACKAGE / "open-repo-control.ps1").read_text(encoding="utf-8")
        self.assertIn("trap {", script)
        self.assertIn("Set-Content -LiteralPath $FailureLogPath", script)
        self.assertIn("exit 1", script)
        self.assertIn("Get-Content -LiteralPath $SshErrorLogPath -Tail 20", script)
        self.assertIn("$sshProcess.ExitCode", script)

    def test_browser_launches_remain_guarded_by_tunnel_only(self) -> None:
        script = (PACKAGE / "open-repo-control.ps1").read_text(encoding="utf-8")
        self.assertEqual(script.count("Start-Process $RepoControlUrl"), 2)
        self.assertEqual(
            script.count("if (-not $TunnelOnly) {\n        Start-Process $RepoControlUrl"),
            1,
        )
        self.assertEqual(
            script.count("if (-not $TunnelOnly) {\n            Start-Process $RepoControlUrl"),
            1,
        )
