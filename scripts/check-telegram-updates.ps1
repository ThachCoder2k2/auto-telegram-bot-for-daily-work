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

if (-not $env:TELEGRAM_BOT_TOKEN) {
    Write-Host "TELEGRAM_BOT_TOKEN is missing in .env." -ForegroundColor Yellow
    exit 1
}

$url = "https://api.telegram.org/bot$($env:TELEGRAM_BOT_TOKEN)/getUpdates"
Invoke-RestMethod -Uri $url -Method Get | ConvertTo-Json -Depth 10

