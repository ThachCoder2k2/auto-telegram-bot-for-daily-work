param(
    [string]$Cron = "0 8 * * *",
    [string]$TimeZone = "Asia/Ho_Chi_Minh"
)

$projectRoot = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $projectRoot ".env"

if (-not (Test-Path -LiteralPath $envFile)) {
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

if (-not $env:TELEGRAM_CHAT_ID) {
    Write-Host "TELEGRAM_CHAT_ID is missing in .env." -ForegroundColor Yellow
    exit 1
}

$message = "Run the Digital Curator and IELTS Mentor workflow in the workspace. Build the 5-module Daily Intelligence Briefing for a Hanoi-based game/web developer: 5-5-5-5 news, inspiration lab, continuity tracker, IELTS mastery, and persistence tag. Send it to Telegram."

openclaw cron add `
  --name "Daily intel digest" `
  --cron $Cron `
  --tz $TimeZone `
  --session isolated `
  --message $message `
  --announce `
  --channel telegram `
  --to $env:TELEGRAM_CHAT_ID
