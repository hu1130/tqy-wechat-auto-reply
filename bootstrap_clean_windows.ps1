param(
  [string]$RepoUrl = "https://github.com/hu1130/tqy-wechat-auto-reply.git",
  [string]$InstallDir = "$env:USERPROFILE\Documents\tqy-wechat-auto-reply",
  [string]$Branch = "main",
  [switch]$SkipTests,
  [switch]$SkipDoctor
)

$ErrorActionPreference = "Stop"

function Write-Step([string]$Message) {
  Write-Host ""
  Write-Host "==> $Message" -ForegroundColor Cyan
}

function Refresh-Path {
  $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  $env:Path = "$machinePath;$userPath"
}

function Ensure-Windows {
  if ($env:OS -ne "Windows_NT") {
    throw "This project requires Windows desktop WeChat and wxauto. Run this script on Windows."
  }
}

function Ensure-Winget {
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw "winget is not available. Install Git and Python 3.11 manually, then rerun this script."
  }
}

function Ensure-Tool([string]$CommandName, [string]$WingetId) {
  if (Get-Command $CommandName -ErrorAction SilentlyContinue) {
    return
  }
  Ensure-Winget
  Write-Step "Installing $CommandName with winget"
  winget install --id $WingetId -e --source winget --accept-package-agreements --accept-source-agreements
  Refresh-Path
  if (-not (Get-Command $CommandName -ErrorAction SilentlyContinue)) {
    throw "$CommandName was installed, but it is still not on PATH. Open a new PowerShell window and rerun."
  }
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
  return @()
}

function Ensure-Python {
  $pythonCommand = Get-PythonCommand
  if ($pythonCommand.Count -gt 0) {
    return $pythonCommand
  }
  Ensure-Winget
  Write-Step "Installing Python 3.11 with winget"
  winget install --id Python.Python.3.11 -e --source winget --accept-package-agreements --accept-source-agreements
  Refresh-Path
  $pythonCommand = Get-PythonCommand
  if ($pythonCommand.Count -eq 0) {
    throw "Python was installed, but it is still not on PATH. Open a new PowerShell window and rerun."
  }
  return $pythonCommand
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

function Clone-Or-UpdateRepo {
  Ensure-Tool "git" "Git.Git"
  $target = Resolve-Path -LiteralPath (Split-Path -Parent $InstallDir) -ErrorAction SilentlyContinue
  if (-not $target) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $InstallDir) | Out-Null
  }

  if (Test-Path -LiteralPath (Join-Path $InstallDir ".git")) {
    Write-Step "Updating existing repository"
    Push-Location $InstallDir
    git fetch --all --prune
    git checkout $Branch
    git pull --ff-only
    Pop-Location
    return
  }

  if (Test-Path -LiteralPath $InstallDir) {
    $items = Get-ChildItem -LiteralPath $InstallDir -Force
    if ($items.Count -gt 0) {
      throw "InstallDir exists and is not empty: $InstallDir"
    }
  }

  Write-Step "Cloning repository"
  git clone --branch $Branch $RepoUrl $InstallDir
}

Ensure-Windows
$pythonCommand = Ensure-Python
Clone-Or-UpdateRepo

Write-Step "Running local setup and tests"
$setupScript = Join-Path $InstallDir "setup_test_environment.ps1"
if (-not (Test-Path -LiteralPath $setupScript)) {
  throw "Missing setup script: $setupScript"
}

$setupArgs = @("-ExecutionPolicy", "Bypass", "-File", $setupScript)
if ($SkipTests) {
  $setupArgs += "-SkipTests"
}
if ($SkipDoctor) {
  $setupArgs += "-SkipDoctor"
}
powershell @setupArgs
if ($LASTEXITCODE -ne 0) {
  throw "setup_test_environment.ps1 failed."
}

Write-Step "Done"
Write-Host "Project path: $InstallDir"
Write-Host "No-WeChat simulation command:"
Write-Host "  cd `"$InstallDir`""
Write-Host "  .\.venv\Scripts\python.exe main.py simulate --reply `"ok, received`" `"hello`""
Write-Host "Next dry-run command:"
Write-Host "  cd `"$InstallDir`""
Write-Host "  .\.venv\Scripts\python.exe main.py run --dry-run --chat 文件传输助手"
