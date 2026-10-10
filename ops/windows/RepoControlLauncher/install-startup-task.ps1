$ErrorActionPreference = "Stop"

$TaskName = "Repo Control SSH Tunnel"
$TaskDescription = "Managed by the RepoControlLauncher package; opens the Repo Control SSH tunnel at user logon."
$LauncherPath = Join-Path $PSScriptRoot "start-tunnel-hidden.vbs"
$ScriptHostPath = Join-Path $env:SystemRoot "System32\wscript.exe"
if (-not (Test-Path -LiteralPath $ScriptHostPath -PathType Leaf)) {
    throw "Windows Script Host (wscript.exe) is required for console-free logon startup."
}
if (-not (Test-Path -LiteralPath $LauncherPath -PathType Leaf)) {
    throw "The portable launcher package is incomplete: $LauncherPath is missing."
}
$CurrentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if ($null -ne $ExistingTask -and $ExistingTask.Description -ne $TaskDescription) {
    throw "A scheduled task named '$TaskName' exists but is not owned by RepoControlLauncher. It was not changed."
}

$Action = New-ScheduledTaskAction `
    -Execute $ScriptHostPath `
    -Argument "//B //Nologo `"$LauncherPath`""
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
$Principal = New-ScheduledTaskPrincipal `
    -UserId $CurrentUser `
    -LogonType Interactive `
    -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description $TaskDescription `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Force | Out-Null

Write-Host "Installed or updated the per-user '$TaskName' logon task."
