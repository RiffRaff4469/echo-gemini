<#
.SYNOPSIS
    Register echo-server as a Windows at-boot task, so the backend is actually
    always-on rather than "on while a terminal window happens to be open".

.DESCRIPTION
    Creates a Task Scheduler task that runs the server at system startup as
    SYSTEM, restarting it if it dies. This is the primary route; NSSM works too
    if you would rather have a real service entry (see NOTES).

    What this script deliberately does NOT do:

      * It does not set GEMINI_API_KEY or ECHO_SHARED_SECRET. Secrets are read
        from the repo-root .env at runtime, or from the machine environment.
        Nothing here writes a secret into a task definition, where it would sit
        in plaintext in the Task Scheduler XML.

      * It does not change power settings. Disabling sleep, hibernate and USB
        selective suspend is a manual step -- see README.md. An always-on
        backend that sleeps is not one, but silently rewriting a household PC's
        power plan is not this script's business.

      * It does not open a firewall rule by default. Use -AddFirewallRule to add
        one scoped to the Tailscale interface.

.PARAMETER TaskName
    Scheduled task name. Default: EchoGeminiServer

.PARAMETER Python
    Python executable to run. Defaults to .venv\Scripts\python.exe in the repo
    if it exists, otherwise whatever `python` resolves to.

.PARAMETER AddFirewallRule
    Also add an inbound allow rule for the listener port, scoped to Private
    profile. Read the note below about scoping it to Tailscale.

.PARAMETER Port
    Port to open in the firewall rule. Default: 8765 (match ECHO_PORT).

.PARAMETER Uninstall
    Remove the task (and the firewall rule, if present) and exit.

.EXAMPLE
    # from an ELEVATED PowerShell, at the repo root
    .\server\install-service.ps1

.EXAMPLE
    .\server\install-service.ps1 -AddFirewallRule -Port 8765

.EXAMPLE
    .\server\install-service.ps1 -Uninstall

.NOTES
    NSSM alternative, if you prefer a real service:

        nssm install EchoGeminiServer "C:\path\to\.venv\Scripts\python.exe" "C:\path\to\server\main.py"
        nssm set EchoGeminiServer AppDirectory "C:\path\to\echo-gemini"
        nssm set EchoGeminiServer AppStdout "C:\path\to\echo-gemini\server\data\service.log"
        nssm set EchoGeminiServer AppStderr "C:\path\to\echo-gemini\server\data\service.log"
        nssm set EchoGeminiServer Start SERVICE_AUTO_START
        nssm start EchoGeminiServer

    Either way the process reads .env from the repo root at startup.
#>

[CmdletBinding()]
param(
    [string]$TaskName = 'EchoGeminiServer',
    [string]$Python,
    [switch]$AddFirewallRule,
    [int]$Port = 8765,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

$RepoRoot  = Split-Path -Parent $PSScriptRoot
$ServerMain = Join-Path $PSScriptRoot 'main.py'
$LogDir    = Join-Path $PSScriptRoot 'data'
$FirewallRuleName = "$TaskName listener"

function Assert-Elevated {
    $identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "This script must be run from an elevated PowerShell (Run as Administrator)."
    }
}

function Resolve-Python {
    if ($Python) {
        if (-not (Test-Path $Python)) { throw "Python not found at: $Python" }
        return (Resolve-Path $Python).Path
    }
    $venv = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    if (Test-Path $venv) { return (Resolve-Path $venv).Path }

    $found = Get-Command python -ErrorAction SilentlyContinue
    if (-not $found) {
        throw "No Python found. Create the venv first:`n  python -m venv .venv`n  .\.venv\Scripts\python.exe -m pip install -r server\requirements.txt"
    }
    Write-Warning "No .venv found; falling back to $($found.Source). The service will use whatever packages are installed there."
    return $found.Source
}

Assert-Elevated

# --- uninstall --------------------------------------------------------------

if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task named '$TaskName'."
    }

    $rule = Get-NetFirewallRule -DisplayName $FirewallRuleName -ErrorAction SilentlyContinue
    if ($rule) {
        Remove-NetFirewallRule -DisplayName $FirewallRuleName
        Write-Host "Removed firewall rule '$FirewallRuleName'."
    }
    return
}

# --- preflight --------------------------------------------------------------

if (-not (Test-Path $ServerMain)) { throw "Cannot find $ServerMain" }
$PythonExe = Resolve-Python
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$envFile = Join-Path $RepoRoot '.env'
if (-not (Test-Path $envFile)) {
    Write-Warning @"
No .env at $envFile.

The server will refuse to start without ECHO_SHARED_SECRET. Create it now:

    Copy-Item .env.example .env
    # then edit .env and set ECHO_SHARED_SECRET to the output of:
    #   $PythonExe -c "import secrets; print(secrets.token_urlsafe(32))"

Registering the task anyway -- it will start correctly once .env exists.
"@
} else {
    $secretLine = Select-String -Path $envFile -Pattern '^\s*ECHO_SHARED_SECRET\s*=\s*\S' -Quiet
    if (-not $secretLine) {
        Write-Warning "ECHO_SHARED_SECRET looks empty in .env; the server will refuse to start."
    }
    if (Select-String -Path $envFile -Pattern '^\s*ECHO_SHARED_SECRET\s*=\s*replace-me' -Quiet) {
        Write-Warning "ECHO_SHARED_SECRET is still the placeholder from .env.example. Generate a real one."
    }
}

Write-Host "Repo       : $RepoRoot"
Write-Host "Python     : $PythonExe"
Write-Host "Entrypoint : $ServerMain"
Write-Host "Logs       : $LogDir\service.log"
Write-Host ""

# --- register the task ------------------------------------------------------

# cmd.exe wrapper only so stdout/stderr land in a log file; Task Scheduler has
# no native output redirection.
$logPath = Join-Path $LogDir 'service.log'
$command = '"{0}" "{1}" >> "{2}" 2>&1' -f $PythonExe, $ServerMain, $logPath

$action = New-ScheduledTaskAction -Execute 'cmd.exe' `
    -Argument ('/c ' + $command) `
    -WorkingDirectory $RepoRoot

$trigger = New-ScheduledTaskTrigger -AtStartup

# SYSTEM so it runs with no one logged in -- the whole point of "always on".
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' `
    -LogonType ServiceAccount -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -RestartCount 999 `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Task '$TaskName' already exists -- replacing it."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description 'echo-gemini backend: WebSocket hub, wake word, Gemini Live, display API.' | Out-Null

Write-Host "Registered scheduled task '$TaskName' (at startup, as SYSTEM, auto-restart)."

# --- optional firewall rule -------------------------------------------------

if ($AddFirewallRule) {
    if (Get-NetFirewallRule -DisplayName $FirewallRuleName -ErrorAction SilentlyContinue) {
        Remove-NetFirewallRule -DisplayName $FirewallRuleName
    }
    New-NetFirewallRule -DisplayName $FirewallRuleName `
        -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port `
        -Profile Private -Program Any | Out-Null
    Write-Host "Added inbound allow rule for TCP $Port on the Private profile."
    Write-Warning @"
That rule covers the whole Private profile, not just Tailscale. To narrow it to
the tailnet, find the Tailscale interface and scope the rule to it:

    Get-NetAdapter | Where-Object { `$_.InterfaceDescription -like '*Tailscale*' }
    Set-NetFirewallRule -DisplayName '$FirewallRuleName' -InterfaceAlias '<that alias>'

Or skip the rule entirely and set ECHO_HOST to the machine's Tailscale IP in
.env, so the listener is never bound to any other interface.
"@
}

# --- start it now -----------------------------------------------------------

Write-Host ""
Write-Host "Starting it now..."
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 4

$info = Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo
Write-Host "  LastTaskResult : $($info.LastTaskResult)  (0 = running/ok, 267009 = currently running)"

try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 5
    Write-Host "  /health        : ok, device_connected=$($health.device_connected), live_enabled=$($health.live_enabled)"
} catch {
    Write-Warning "  /health did not respond on port $Port. Check $logPath -- the usual cause is a missing ECHO_SHARED_SECRET."
}

Write-Host ""
Write-Host "Manage it with:"
Write-Host "  Get-ScheduledTaskInfo -TaskName $TaskName"
Write-Host "  Stop-ScheduledTask    -TaskName $TaskName"
Write-Host "  Start-ScheduledTask   -TaskName $TaskName"
Write-Host "  Get-Content '$logPath' -Tail 50 -Wait"
Write-Host "  .\server\install-service.ps1 -Uninstall"
Write-Host ""
Write-Host "STILL TO DO BY HAND (see README.md): disable sleep, hibernate and USB"
Write-Host "selective suspend. This script does not touch your power plan."
