# Register (or repair) the VSW workbench watchdog as a scheduled task.
#
# Why PowerShell instead of `schtasks /Create`:
#   * /TR has a hard 261-character limit, so the old .bat route had to go through
#     cmd.exe -- and cmd.exe is a console program: every start flashes a black
#     window on the user's screen.
#   * /SC ONLOGON writes <StopOnIdleEnd>true</StopOnIdleEnd>, which lets Task
#     Scheduler stop the watchdog as soon as the machine stops being idle -- a
#     watchdog that gets switched off exactly when you sit down is not a watchdog.
#
# So: the action points straight at pythonw.exe (no console host involved at all),
# and the settings are spelled out explicitly.
#
# Usage (normally called by install_watchdog.bat):
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\vsw-install-task.ps1 -Mode user
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\vsw-install-task.ps1 -Mode system

[CmdletBinding()]
param(
    [ValidateSet('user', 'system')]
    [string]$Mode = 'user',
    [string]$Root = '',
    [string]$Port = '8799',
    [string]$Repo = 'silentwinter127-cv2/vsw-workbench',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $here
if (-not $Root) { $Root = Join-Path $repoRoot 'vsw\data' }

$taskName = 'VSW Workbench Watchdog'
if ($Mode -eq 'system') { $taskName = 'VSW Workbench Watchdog (boot)' }

function Find-Python {
    param([string]$Name)
    foreach ($dir in ($env:PATH -split ';')) {
        if (-not $dir) { continue }
        $candidate = Join-Path $dir $Name
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return $null
}

if ($Remove) {
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "[vsw] removed task: $taskName"
    } else {
        Write-Host "[vsw] no such task: $taskName"
    }
    return
}

$pythonw = Find-Python 'pythonw.exe'
$python = Find-Python 'python.exe'
if (-not $pythonw) { $pythonw = $python }
if (-not $pythonw) { throw 'python.exe / pythonw.exe not found on PATH. Install Python 3.10+ first.' }

$watchdog = Join-Path $repoRoot 'scripts\vsw-watchdog.py'
if (-not (Test-Path -LiteralPath $watchdog)) { throw "watchdog script not found: $watchdog" }

$arguments = @(
    '-u'
    "`"$watchdog`""
    '--root', "`"$Root`""
    '--host', '0.0.0.0'
    '--port', $Port
    '--gh', $Repo
) -join ' '

$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $repoRoot

if ($Mode -eq 'system') {
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
} else {
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $principal = New-ScheduledTaskPrincipal -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) `
        -LogonType Interactive -RunLevel Highest
}

# Hidden + no idle stop + no battery stop + unlimited run time.
# (-Hidden keeps the task out of the task list UI; the important bit for
#  "black window flashes" is that the action is pythonw.exe, not cmd.exe.)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -Hidden `
    -DontStopOnIdleEnd

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "[vsw] registered: $taskName"
Write-Host "      action : `"$pythonw`" $arguments"
Write-Host "      trigger: $(if ($Mode -eq 'system') { 'at startup (SYSTEM, no login needed)' } else { 'at logon (current user)' })"
Write-Host "      root   : $Root"
Write-Host ""

$task = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName
Write-Host ("      state  : {0} / last result {1}" -f $task.State, $info.LastTaskResult)
Write-Host ("      idle   : StopOnIdleEnd={0} (must be False)" -f $task.Settings.IdleSettings.StopOnIdleEnd)
Write-Host ""
Write-Host "Start it now:  schtasks /Run /TN `"$taskName`""
Write-Host "Logs:   $Root\logs\watchdog.log"
Write-Host "Status: $Root\logs\watchdog-status.json"
