# ─────────────────────────────────────────────────────────────────────────────
# run.ps1  —  Airline Schedule Intelligence Dashboard startup script
#
# Usage:  .\run.ps1 [-Port 8000] [-DataFolder "C:\path\to\workset\out"]
#
# Requirements:
#   • Python 3.11+  (python --version)
#   • pip packages  (run once: pip install -r requirements.txt)
#   • A valid .env  (copy .env.example → .env and fill in your values)
# ─────────────────────────────────────────────────────────────────────────────

param(
    [int]    $Port       = 8000,
    [string] $DataFolder = ""       # overrides SCHEDAI_DATA_FOLDER in .env
)

$ErrorActionPreference = "Stop"
$AppDir = $PSScriptRoot   # directory containing this script

# ── 1. Validate Python ────────────────────────────────────────────────────────
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Error "Python not found. Install Python 3.11+ and ensure it is on your PATH."
    exit 1
}
$pyVer = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
Write-Host "✔ Python $pyVer found at $($py.Source)" -ForegroundColor Green

# ── 2. Validate .env ──────────────────────────────────────────────────────────
$envFile = Join-Path $AppDir ".env"
if (-not (Test-Path $envFile)) {
    Write-Error ".env file not found. Copy .env.example to .env and fill in your values."
    exit 1
}
Write-Host "✔ .env found" -ForegroundColor Green

# ── 3. Install / verify dependencies ─────────────────────────────────────────
Write-Host "Checking Python dependencies …" -ForegroundColor Cyan
& python -m pip install -r (Join-Path $AppDir "requirements.txt") --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Error "pip install failed. See output above."
    exit 1
}
Write-Host "✔ Dependencies OK" -ForegroundColor Green

# ── 4. Optional: override data folder ────────────────────────────────────────
$env:SCHEDAI_DATA_FOLDER = if ($DataFolder) { $DataFolder } else { $env:SCHEDAI_DATA_FOLDER }

# ── 5. Kill any existing process on the same port ────────────────────────────
$existing = netstat -ano | Select-String ":$Port\s" | Select-String "LISTENING"
if ($existing) {
    $existingPid = ($existing -split "\s+")[-1]
    Write-Host "Port $Port in use by PID $existingPid — stopping it …" -ForegroundColor Yellow
    Stop-Process -Id $existingPid -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

# ── 6. Start the server ───────────────────────────────────────────────────────
$logOut = Join-Path $AppDir "server_live.log"
$logErr = Join-Path $AppDir "server_live.err"

Write-Host ""
Write-Host "Starting Airline Schedule Intelligence Dashboard …" -ForegroundColor Cyan
Write-Host "  URL      : http://localhost:$Port" -ForegroundColor White
Write-Host "  API Docs : http://localhost:$Port/docs" -ForegroundColor White
Write-Host "  Log      : $logOut" -ForegroundColor White
Write-Host ""

$proc = Start-Process -FilePath "python" `
    -ArgumentList "-m uvicorn app.main:app --host 0.0.0.0 --port $Port" `
    -WorkingDirectory $AppDir `
    -RedirectStandardOutput $logOut `
    -RedirectStandardError  $logErr `
    -PassThru -WindowStyle Hidden

Write-Host "✔ Server started (PID $($proc.Id))" -ForegroundColor Green
Write-Host "  To stop : Stop-Process -Id $($proc.Id)" -ForegroundColor Gray
Write-Host ""

# ── 7. Wait for startup and tail the log ─────────────────────────────────────
Write-Host "Waiting for startup …" -ForegroundColor Cyan
$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    $ready = Get-Content $logOut -ErrorAction SilentlyContinue |
             Select-String "Startup complete" -Quiet
    if ($ready) { break }
    $failed = Get-Process -Id $proc.Id -ErrorAction SilentlyContinue
    if (-not $failed) {
        Write-Error "Server process exited unexpectedly. Check $logErr for details."
        Get-Content $logErr -Tail 20 | Write-Host -ForegroundColor Red
        exit 1
    }
}

if ($ready) {
    Write-Host "✔ Startup complete — dashboard is live at http://localhost:$Port" -ForegroundColor Green
} else {
    Write-Warning "Startup not confirmed within 30 s — check $logOut for status."
}

Write-Host ""
Write-Host "Last log lines:" -ForegroundColor Cyan
Get-Content $logOut -Tail 10
