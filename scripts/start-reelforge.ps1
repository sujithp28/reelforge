# Start the ReelForge API and the website with one command.
# From the repository root:  powershell -File scripts/start-reelforge.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$backend = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"

if (-not $env:REELFORGE_DATA_DIR) {
  $env:REELFORGE_DATA_DIR = Join-Path $backend "data"
}

$api = Start-Process -FilePath "python" -WorkingDirectory $backend -PassThru -ArgumentList @(
  "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"
)
$web = Start-Process -FilePath "npm" -WorkingDirectory $frontend -PassThru -ArgumentList @(
  "run", "dev", "--", "--hostname", "127.0.0.1", "--port", "3000"
)

Write-Host "ReelForge API:  http://127.0.0.1:8000"
Write-Host "ReelForge site: http://127.0.0.1:3000"
Write-Host "Press Enter to stop both."
[void] (Read-Host)
foreach ($proc in @($web, $api)) {
  if ($proc -and -not $proc.HasExited) {
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
  }
}
