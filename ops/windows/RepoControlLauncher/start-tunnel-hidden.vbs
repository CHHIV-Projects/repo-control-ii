Option Explicit

Dim shell, files, launcherPath, powerShellPath, command, result
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
launcherPath = files.BuildPath(files.GetParentFolderName(WScript.ScriptFullName), "open-repo-control.ps1")
powerShellPath = shell.ExpandEnvironmentStrings("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")
command = """" & powerShellPath & """ -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File """ & launcherPath & """ -TunnelOnly"
result = shell.Run(command, 0, True)
WScript.Quit result
