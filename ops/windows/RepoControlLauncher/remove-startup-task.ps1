$ErrorActionPreference = "Stop"

$TaskName = "Repo Control SSH Tunnel"
$TaskDescription = "Managed by the RepoControlLauncher package; opens the Repo Control SSH tunnel at user logon."
$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if ($null -eq $ExistingTask) {
    Write-Host "The RepoControlLauncher startup task is not installed."
    return
}

if ($ExistingTask.Description -ne $TaskDescription) {
    throw "A scheduled task named '$TaskName' exists but is not owned by RepoControlLauncher. It was not removed."
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed the RepoControlLauncher per-user startup task."
