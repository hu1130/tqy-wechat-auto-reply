param(
  [switch]$SkipTests,
  [switch]$SkipDoctor
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Write-Step([string]$Message) {
  Write-Host ""
  Write-Host "==> $Message" -ForegroundColor Cyan
}

function Get-PythonCommand {
  if (Get-Command py -ErrorAction SilentlyContinue) {
    try {
      py -3.11 --version | Out-Null
      return @("py", "-3.11")
    } catch {
    }
  }
  if (Get-Command python -ErrorAction SilentlyContinue) {
    python --version | Out-Null
    return @("python")
  }
  throw "Python 3.9+ is required. Install Python 3.11 and rerun."
}

function Invoke-SelectedPython([string[]]$PythonCommand, [string[]]$Arguments) {
  if ($PythonCommand.Count -eq 2) {
    & $PythonCommand[0] $PythonCommand[1] @Arguments
  } else {
    & $PythonCommand[0] @Arguments
  }
  if ($LASTEXITCODE -ne 0) {
    throw "Python command failed: $($Arguments -join ' ')"
  }
}

if ($env:OS -ne "Windows_NT") {
  throw "This project requires Windows desktop WeChat 4.x and wxauto4. Run this script on Windows."
}

$pythonCommand = Get-PythonCommand

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
  Write-Step "Creating virtual environment"
  Invoke-SelectedPython -PythonCommand $pythonCommand -Arguments @("-m", "venv", ".venv")
}

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

Write-Step "Installing dependencies"
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
  throw "Failed to upgrade pip."
}
& $venvPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
  throw "Failed to install requirements."
}

if (-not (Test-Path -LiteralPath ".env")) {
  Write-Step "Creating .env from safe defaults"
  & $venvPython main.py init --yes
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to create .env."
  }
}

if (-not $SkipTests) {
  Write-Step "Running unit tests"
  & $venvPython -m unittest discover -s tests -v
  if ($LASTEXITCODE -ne 0) {
    throw "Unit tests failed."
  }
}

if (-not $SkipDoctor) {
  Write-Step "Running doctor without LLM endpoint check"
  & $venvPython main.py doctor --skip-llm
  if ($LASTEXITCODE -ne 0) {
    throw "Doctor failed. Fix the items above before running the bot."
  }
}

Write-Step "Running no-WeChat simulation"
& $venvPython main.py simulate --reply "ok, received" "hello"
if ($LASTEXITCODE -ne 0) {
  throw "No-WeChat simulation failed."
}

Write-Step "Environment is ready"
Write-Host "No-WeChat simulation:"
Write-Host "  .\.venv\Scripts\python.exe main.py simulate --reply `"ok, received`" `"hello`""
Write-Host ""
Write-Host "Dry-run test:"
Write-Host "  .\.venv\Scripts\python.exe main.py run --dry-run --chat 文件传输助手"
Write-Host ""
Write-Host "Before live mode, open and log in to Windows desktop WeChat."
