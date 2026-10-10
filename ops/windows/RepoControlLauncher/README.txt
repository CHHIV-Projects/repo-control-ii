Repo Control Windows Launcher
=============================

Copy the entire RepoControlLauncher folder anywhere on your Windows PC.

On-demand use
-------------

Double-click RepoControl.cmd. It reuses a healthy local Repo Control tunnel or
starts a loopback-only SSH forward, waits for readiness, and opens the browser
at http://127.0.0.1:8765/.
The long-lived SSH tunnel runs without a visible console window.

Optional tunnel at Windows logon
--------------------------------

Run install-startup-task.ps1 once from PowerShell to create or update the
per-user "Repo Control SSH Tunnel" Task Scheduler task. The task runs at user
logon and invokes the launcher with -TunnelOnly; it does not open the browser.
Both PowerShell and SSH run hidden for the logon task. Re-run the installer
after updating this package to apply the console-free task entry point.
The task starts wscript.exe (a non-console Windows host), which runs the small
start-tunnel-hidden.vbs wrapper. It creates PowerShell hidden, waits for the
helper, and returns its exit code to Task Scheduler. The on-demand .cmd
launcher is unchanged.
No administrator rights should be needed for this interactive per-user task.
Keep the folder at its installed location while using the task; if you move the
folder, remove and reinstall the task.

Run remove-startup-task.ps1 to remove only the task owned by this package.
The launcher scripts do not change unrelated scheduled tasks or SSH settings.

Requirements
------------

- Windows OpenSSH client (ssh.exe)
- Windows Script Host and VBScript enabled for optional logon startup
- Network access to henderson-server1
- Valid normal SSH authentication and host-key verification for
  chuck@henderson-server1

Hidden startup uses OpenSSH BatchMode to avoid unseen interactive prompts.
Complete first-time host-key verification using normal SSH in a terminal.
Use your normal non-interactive SSH authentication (such as an available
SSH agent/key). Password/passphrase prompts cannot be answered in hidden
windows; they fail rather than hang. Host-key checking is not disabled.

Failures
--------

On-demand failures are reported in the launcher console. Hidden logon failures
return exit code 1 (Task Scheduler Last Run Result) and write details to:

    %LOCALAPPDATA%\RepoControlLauncher\last-failure.txt

Each new tunnel also has a separate SSH stderr log in that directory; its
path is printed for on-demand use. A previous failure file is historical,
not a current health indicator. Inspect the health endpoint or Task Scheduler
result to determine current status. Logs are local diagnostics, not part of
the portable package; do not copy them when sharing the launcher folder.
If Windows Script Host/VBScript is disabled or unavailable, the scheduled task
fails with a nonzero result before the helper can write last-failure.txt.
Inspect Task Scheduler history in that case. VBScript is deprecated on newer
Windows releases; no feature installation or policy change is performed by
this package. On-demand use does not require VBScript.

This package contains no passwords or private keys. It does not disable SSH
host-key checking, install a Windows service, require a compiled executable,
or use third-party modules.

If local port 8765 is occupied by something other than a healthy Repo Control
endpoint, the launcher fails rather than replacing that listener. When the
launcher starts a tunnel, it prints the process ID and command to stop it. If
readiness times out, the launcher stops only the SSH process it started:

    Stop-Process -Id <PID>
