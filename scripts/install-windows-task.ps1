param(
    [string]$TaskName = "Clawbot Daily Intel Digest",
    [string]$RunAt = "08:00"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$taskScript = Join-Path $PSScriptRoot "run-daily-digest-task.ps1"
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

if (-not (Test-Path -LiteralPath $taskScript)) {
    Write-Host "Task runner script not found: $taskScript" -ForegroundColor Yellow
    exit 1
}

$timeParts = $RunAt -split ":", 2
if ($timeParts.Count -ne 2) {
    throw "RunAt must look like HH:mm"
}

$triggerTime = Get-Date
$triggerTime = $triggerTime.Date.AddHours([int]$timeParts[0]).AddMinutes([int]$timeParts[1])

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$taskScript`"" `
    -WorkingDirectory $projectRoot

$trigger = New-ScheduledTaskTrigger -Daily -At $triggerTime
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName

[pscustomobject]@{
    TaskName = $task.TaskName
    State = $task.State.ToString()
    NextRunTime = $info.NextRunTime
    LastRunTime = $info.LastRunTime
    LastTaskResult = $info.LastTaskResult
    User = $currentUser
    RunAt = $RunAt
} | ConvertTo-Json -Compress
