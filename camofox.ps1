# Start / stop / check the stealth article fetcher (tools\camofox-browser, a plain Node process on 127.0.0.1:9377 - no Docker).
#   .\camofox.ps1 start | stop | status
param([Parameter(Position = 0)][ValidateSet("start", "stop", "status")][string]$Action = "status")

$dir = Join-Path $PSScriptRoot "tools\camofox-browser"
$conn = Get-NetTCPConnection -LocalPort 9377 -State Listen -ErrorAction SilentlyContinue

switch ($Action) {
    "start" {
        if ($conn) { Write-Host "Already running (pid $($conn.OwningProcess))."; break }
        if (-not (Test-Path "$dir\node_modules")) { Write-Host "Not installed: cd tools\camofox-browser; npm install --omit=dev" -ForegroundColor Yellow; break }
        $env:CAMOFOX_BIND_HOST = "127.0.0.1"; $env:CAMOFOX_CRASH_REPORT_ENABLED = "false"
        Start-Process -FilePath node -ArgumentList "server.js" -WorkingDirectory $dir -WindowStyle Hidden
        Start-Sleep 8
        Write-Host "Started on 127.0.0.1:9377."
    }
    "stop" {
        if ($conn) {
            Get-Process -Id $conn.OwningProcess | Stop-Process -Force
            Get-Process camoufox -ErrorAction SilentlyContinue | Stop-Process -Force
            Write-Host "Stopped. Articles that block bots will be skipped until you start it again."
        } else { Write-Host "Not running." }
    }
    "status" {
        if ($conn) {
            $h = Invoke-RestMethod http://127.0.0.1:9377/health -ErrorAction SilentlyContinue
            Write-Host "Running (pid $($conn.OwningProcess)), $($h.memory.rssMb) MB, $($h.activeTabs) tabs."
        } else { Write-Host "Not running." }
    }
}
