# ContextLens launcher — builds the frontend if needed, then serves the app + API on http://localhost:8000
#   .\start.ps1            start
#   .\start.ps1 -Rebuild   force a frontend rebuild first
param([switch]$Rebuild)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = Join-Path $PSScriptRoot "venv\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

if ($Rebuild -or -not (Test-Path "frontend\dist\index.html")) {
    Write-Host "Building frontend..." -ForegroundColor Cyan
    Push-Location frontend
    if (-not (Test-Path "node_modules")) { npm install }
    npm run build
    Pop-Location
}

if (-not (Test-Path "backend\.env") -and (Test-Path "backend\.env.example")) {
    Copy-Item "backend\.env.example" "backend\.env"
    Write-Host "Created backend\.env from the example - edit it if your LM Studio port or models differ." -ForegroundColor Yellow
}

# Optional stealth browser for articles that refuse plain fetches (bot walls). Skipped if not installed or already running.
$cf = Join-Path $PSScriptRoot "tools\camofox-browser"
if ((Test-Path "$cf\node_modules") -and -not (Get-NetTCPConnection -LocalPort 9377 -State Listen -ErrorAction SilentlyContinue)) {
    $env:CAMOFOX_BIND_HOST = "127.0.0.1"; $env:CAMOFOX_CRASH_REPORT_ENABLED = "false"
    Start-Process -FilePath node -ArgumentList "server.js" -WorkingDirectory $cf -WindowStyle Hidden
    Write-Host "Started the stealth article fetcher on 127.0.0.1:9377" -ForegroundColor DarkGray
}

Write-Host "ContextLens -> http://localhost:8000  (LM Studio server + SearXNG should be running)" -ForegroundColor Green
& $py -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
