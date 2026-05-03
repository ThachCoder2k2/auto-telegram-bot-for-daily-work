Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $projectRoot ".env"
$logsDir = Join-Path $projectRoot "logs"
$dateStamp = Get-Date -Format "yyyy-MM-dd"
$logFile = Join-Path $logsDir "daily-digest-$dateStamp.log"

New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

function Write-LogLine {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $logFile -Value "[$timestamp] $Message"
}

Write-LogLine "run-start"

if (-not (Test-Path -LiteralPath $envFile)) {
    Write-LogLine ".env not found"
    Write-Host ".env not found. Copy .env.example to .env first." -ForegroundColor Yellow
    exit 1
}

Get-Content -LiteralPath $envFile | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#")) {
        return
    }

    $parts = $line -split "=", 2
    if ($parts.Count -eq 2) {
        [System.Environment]::SetEnvironmentVariable($parts[0], $parts[1], "Process")
    }
}

$env:PYTHONPATH = Join-Path $projectRoot "src"
Write-LogLine "sending-live-digest"

$output = & python -m daily_intel_bot.main --send-digest 2>&1
$exitCode = $LASTEXITCODE

if ($output) {
    $output | ForEach-Object {
        Write-LogLine "$_"
        Write-Output $_
    }
}

Write-LogLine "run-finish exit_code=$exitCode"
exit $exitCode
