[CmdletBinding()]
param(
    [switch]$TunnelOnly
)

$ErrorActionPreference = "Stop"

$RepoControlUrl = "http://127.0.0.1:8765/"
$RepoControlHealthUrl = "http://127.0.0.1:8765/healthz"
$SshDestination = "chuck@henderson-server1"
$LocalBindAddress = "127.0.0.1"
$LocalPort = 8765
$RemoteAddress = "127.0.0.1"
$RemotePort = 8765
$ReadinessTimeoutSeconds = 20
$SshKeepAliveIntervalSeconds = 30
$SshKeepAliveCountMax = 3
$DiagnosticDirectory = Join-Path $env:LOCALAPPDATA "RepoControlLauncher"
$FailureLogPath = Join-Path $DiagnosticDirectory "last-failure.txt"

trap {
    $failureMessage = $_.ToString()
    New-Item -ItemType Directory -Path $DiagnosticDirectory -Force | Out-Null
    "$(Get-Date -Format o)`r`n$failureMessage" | Set-Content -LiteralPath $FailureLogPath -Encoding UTF8
    Write-Error "$failureMessage`nFailure details: $FailureLogPath" -ErrorAction Continue
    exit 1
}

function Get-RepoControlHealth {
    try {
        $health = Invoke-RestMethod -Uri $RepoControlHealthUrl -TimeoutSec 2
        if ($health.service -eq "repo-control") {
            return $health
        }
    }
    catch {
        return $null
    }
    return $null
}

$health = Get-RepoControlHealth
if ($null -ne $health) {
    if (-not $TunnelOnly) {
        Start-Process $RepoControlUrl
        Write-Host "Opened Repo Control using the existing healthy local access path."
    }
    else {
        Write-Host "Repo Control access is already available."
    }
    return
}

$listener = Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue
if ($null -ne $listener) {
    throw "Local port $LocalPort is occupied but does not serve Repo Control. Close or reconfigure that listener before continuing."
}

$sshCommand = Get-Command ssh.exe -ErrorAction Stop
$sshArguments = @(
    "-N",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=$SshKeepAliveIntervalSeconds",
    "-o", "ServerAliveCountMax=$SshKeepAliveCountMax",
    "-L", "${LocalBindAddress}:${LocalPort}:${RemoteAddress}:${RemotePort}",
    $SshDestination
)
New-Item -ItemType Directory -Path $DiagnosticDirectory -Force | Out-Null
$SshErrorLogPath = Join-Path $DiagnosticDirectory "ssh-$([guid]::NewGuid().ToString('N')).stderr.log"
$sshProcess = Start-Process -FilePath $sshCommand.Source -ArgumentList $sshArguments `
    -WindowStyle Hidden -RedirectStandardError $SshErrorLogPath -PassThru

$deadline = (Get-Date).AddSeconds($ReadinessTimeoutSeconds)
do {
    Start-Sleep -Milliseconds 500
    if ($sshProcess.HasExited) {
        $sshDiagnostic = Get-Content -LiteralPath $SshErrorLogPath -Tail 20 -ErrorAction Stop | Out-String
        throw "SSH tunnel exited with code $($sshProcess.ExitCode) before Repo Control became available. $sshDiagnostic`nSSH diagnostics: $SshErrorLogPath. Complete normal SSH host-key verification and authentication in a terminal; hidden tunnels require non-interactive authentication (for example, an available SSH agent/key)."
    }
    $health = Get-RepoControlHealth
    if ($null -ne $health) {
        if (-not $TunnelOnly) {
            Start-Process $RepoControlUrl
            Write-Host "Repo Control is open."
        }
        else {
            Write-Host "Repo Control tunnel is ready."
        }
        Write-Host "SSH tunnel process ID: $($sshProcess.Id)"
        Write-Host "To stop this tunnel: Stop-Process -Id $($sshProcess.Id)"
        Write-Host "SSH diagnostics: $SshErrorLogPath"
        return
    }
} while ((Get-Date) -lt $deadline)

if (-not $sshProcess.HasExited) {
    Stop-Process -Id $sshProcess.Id -ErrorAction Stop
}
throw "Timed out waiting for Repo Control on 127.0.0.1:$LocalPort. The SSH tunnel process started by this launcher (PID $($sshProcess.Id)) was stopped. Check network access and server status. SSH diagnostics: $SshErrorLogPath"
