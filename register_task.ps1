# Registers (or updates) a Windows Scheduled Task that runs the daily forex-ai
# analysis every day at 6:00am local (NZT) time.
#
#   Run once:   powershell -ExecutionPolicy Bypass -File .\register_task.ps1
#   Remove:     Unregister-ScheduledTask -TaskName 'ForexAI-Daily' -Confirm:$false
#   Inspect:    Get-ScheduledTask -TaskName 'ForexAI-Daily'
#   Test now:   Start-ScheduledTask -TaskName 'ForexAI-Daily'
#
# The task runs only while you are logged in (no stored password required).

$ErrorActionPreference = 'Stop'
$taskName = 'ForexAI-Daily'
$root     = $PSScriptRoot
$script   = Join-Path $root 'daily.py'

if (-not (Test-Path $script)) { throw "daily.py not found in $root" }

# Resolve the REAL interpreter via sys.executable. (Get-Command python) often
# returns the WindowsApps app-execution alias, which can fail under Task Scheduler.
$python = (& python -c "import sys; print(sys.executable)").Trim()
if (-not (Test-Path $python) -or $python -like '*WindowsApps*') {
    throw "Could not resolve a real python.exe (got '$python'). Set `$python manually in this script."
}

Write-Host "Python : $python"
Write-Host "Script : $script"

$action  = New-ScheduledTaskAction -Execute $python -Argument "`"$script`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At 6:00am
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
            -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description 'Daily forex-ai analysis of the watchlist at 6am' -Force | Out-Null

Write-Host "`nRegistered '$taskName' to run daily at 6:00am." -ForegroundColor Green
Write-Host "Test it now with:  Start-ScheduledTask -TaskName '$taskName'"
