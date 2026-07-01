$ErrorActionPreference = "Stop"

Write-Host "== WeChat AI setup =="

try {
  py -3.11 --version | Out-Null
  $pythonLauncher = "py -3.11"
} catch {
  $pythonLauncher = "py -3"
}

if (!(Test-Path ".venv")) {
  Write-Host "Creating virtual environment..."
  Invoke-Expression "$pythonLauncher -m venv .venv"
}

$python = ".\.venv\Scripts\python.exe"
$pip = ".\.venv\Scripts\pip.exe"

Write-Host "Installing dependencies..."
& $python -m pip install --upgrade pip
& $pip install -r requirements.txt

if (!(Test-Path ".env")) {
  Write-Host "Creating default .env..."
  & $python main.py init --yes
} else {
  Write-Host ".env already exists. Keeping it."
}

Write-Host ""
Write-Host "Setup complete."
Write-Host "Next:"
Write-Host "  1. Edit .env if needed"
Write-Host "  2. Run: .\.venv\Scripts\python.exe main.py doctor"
Write-Host "  3. Run: .\start_dry_run.bat"
