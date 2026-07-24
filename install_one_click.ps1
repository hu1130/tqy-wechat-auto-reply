param(
  [string]$InstallDir = "$env:USERPROFILE\Documents\tqy-wechat-auto-reply",
  [switch]$InstallWeChat,
  [switch]$ConfigureApi,
  [switch]$SkipUnitTests,
  [switch]$NoShortcuts
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$PackageRoot = $PSScriptRoot
$ProjectSource = $PackageRoot
$InstallLog = Join-Path $env:TEMP ("tqy-wechat-auto-reply-install-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".log")

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
    throw "This installer must run on Windows."
  }
}

function Ensure-Winget {
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw "winget is not available. Install Python 3.11 manually, then run install_one_click.bat again."
  }
}

function Invoke-CheckedCommand([string]$Command, [string[]]$Arguments, [string]$FailureMessage) {
  & $Command @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "$FailureMessage Exit code: $LASTEXITCODE"
  }
}

function Invoke-SelectedPython([string[]]$PythonCommand, [string[]]$Arguments, [string]$FailureMessage) {
  $commandParts = @($PythonCommand)
  $command = $commandParts[0]
  $prefixArgs = @()
  if ($commandParts.Count -gt 1) {
    $prefixArgs = $commandParts[1..($commandParts.Count - 1)]
  }
  & $command @prefixArgs @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "$FailureMessage Exit code: $LASTEXITCODE"
  }
}

function Test-SelectedPython([string[]]$PythonCommand) {
  $commandParts = @($PythonCommand)
  $command = $commandParts[0]
  $prefixArgs = @()
  if ($commandParts.Count -gt 1) {
    $prefixArgs = $commandParts[1..($commandParts.Count - 1)]
  }
  & $command @prefixArgs -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)" >$null 2>$null
  return $LASTEXITCODE -eq 0
}

function Find-PythonCommand {
  $candidates = @(
    @("py", "-3.11"),
    @("python")
  )
  foreach ($candidate in $candidates) {
    if (-not (Get-Command $candidate[0] -ErrorAction SilentlyContinue)) {
      continue
    }
    if (Test-SelectedPython -PythonCommand $candidate) {
      return $candidate
    }
  }
  return @()
}

function Ensure-Python {
  $pythonCommand = @(Find-PythonCommand)
  if (@($pythonCommand).Count -gt 0) {
    return $pythonCommand
  }

  Ensure-Winget
  Write-Step "Installing Python 3.11 with winget"
  Invoke-CheckedCommand "winget" @(
    "install",
    "--id", "Python.Python.3.11",
    "-e",
    "--source", "winget",
    "--accept-package-agreements",
    "--accept-source-agreements"
  ) "Failed to install Python 3.11."

  Refresh-Path
  $pythonCommand = @(Find-PythonCommand)
  if (@($pythonCommand).Count -eq 0) {
    throw "Python was installed, but it is not available in this shell. Open a new PowerShell window and run install_one_click.bat again."
  }
  return $pythonCommand
}

function Install-WeChatIfRequested {
  if (-not $InstallWeChat) {
    return
  }
  $compatibleInstaller = Join-Path $PackageRoot "install_wechat_compatible.ps1"
  if (-not (Test-Path -LiteralPath $compatibleInstaller)) {
    throw "Missing install_wechat_compatible.ps1."
  }

  Write-Step "Installing compatible Windows WeChat"
  Invoke-CheckedCommand "powershell" @(
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    $compatibleInstaller
  ) "Failed to install compatible Windows WeChat."
}

function Test-SamePath([string]$PathA, [string]$PathB) {
  try {
    $resolvedA = (Resolve-Path -LiteralPath $PathA).Path.TrimEnd("\")
    $resolvedB = (Resolve-Path -LiteralPath $PathB).Path.TrimEnd("\")
    return $resolvedA -ieq $resolvedB
  } catch {
    return $false
  }
}

function Copy-ProjectToInstallDir {
  if (-not (Test-Path -LiteralPath (Join-Path $ProjectSource "main.py"))) {
    throw "Installer files are incomplete. main.py was not found next to install_one_click.ps1."
  }

  $parent = Split-Path -Parent $InstallDir
  if ($parent -and -not (Test-Path -LiteralPath $parent)) {
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
  }
  if (-not (Test-Path -LiteralPath $InstallDir)) {
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
  }

  if (Test-SamePath $ProjectSource $InstallDir) {
    Write-Step "Using current folder as install directory"
    return
  }

  Write-Step "Copying project files to $InstallDir"
  $excludeDirs = @(".venv", "__pycache__", ".pytest_cache", ".git", "logs", "data", "reports")
  $excludeFiles = @("*.pyc")
  $copyArgs = @($ProjectSource, $InstallDir, "/E", "/XD") + $excludeDirs + @("/XF") + $excludeFiles + @(
    "/R:2",
    "/W:1",
    "/NFL",
    "/NDL",
    "/NJH",
    "/NJS",
    "/NP"
  )
  & robocopy @copyArgs
  $copyExitCode = $LASTEXITCODE
  $global:LASTEXITCODE = 0
  if ($copyExitCode -gt 7) {
    throw "Failed to copy project files. Robocopy exit code: $copyExitCode"
  }
}

function Ensure-Venv([string[]]$PythonCommand) {
  Set-Location -LiteralPath $InstallDir
  if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    Write-Step "Creating virtual environment"
    Invoke-SelectedPython -PythonCommand $PythonCommand -Arguments @("-m", "venv", ".venv") -FailureMessage "Failed to create virtual environment."
  }
}

function Install-PythonDependencies {
  $venvPython = Join-Path $InstallDir ".venv\Scripts\python.exe"
  Write-Step "Installing Python dependencies"
  Invoke-CheckedCommand $venvPython @("-m", "pip", "install", "--upgrade", "pip") "Failed to upgrade pip."

  $pipArgs = @("-m", "pip", "install")
  $wheelDir = Join-Path $InstallDir "vendor\wheels"
  if (Test-Path -LiteralPath $wheelDir) {
    $wheelFiles = @(Get-ChildItem -LiteralPath $wheelDir -File -ErrorAction SilentlyContinue)
    if ($wheelFiles.Count -gt 0) {
      $pipArgs += @("--find-links", $wheelDir)
    }
  }
  $pipArgs += @("-r", "requirements.txt")
  Invoke-CheckedCommand $venvPython $pipArgs "Failed to install requirements."
}

function Initialize-EnvFile {
  $venvPython = Join-Path $InstallDir ".venv\Scripts\python.exe"
  if (-not (Test-Path -LiteralPath ".env")) {
    Write-Step "Creating .env from safe defaults"
    Invoke-CheckedCommand $venvPython @("main.py", "init", "--yes") "Failed to create .env."
  } else {
    Write-Step "Keeping existing .env"
  }
}

function Convert-SecureStringToPlainText([securestring]$Value) {
  $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
  try {
    return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
  } finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
  }
}

function Set-EnvValue([string]$Path, [string]$Name, [string]$Value) {
  $lines = @()
  if (Test-Path -LiteralPath $Path) {
    $lines = @(Get-Content -LiteralPath $Path -Encoding UTF8)
  }
  $escaped = $Value -replace "`r", "" -replace "`n", ""
  $updated = $false
  for ($index = 0; $index -lt $lines.Count; $index++) {
    if ($lines[$index] -match ("^\s*" + [regex]::Escape($Name) + "\s*=")) {
      $lines[$index] = "$Name=$escaped"
      $updated = $true
      break
    }
  }
  if (-not $updated) {
    $lines += "$Name=$escaped"
  }
  Set-Content -LiteralPath $Path -Value $lines -Encoding UTF8
}

function Configure-ColleagueApi {
  if (-not $ConfigureApi) {
    return
  }
  Write-Step "Configuring colleague API"
  Write-Host "If you use your colleague's API, paste its base URL now."
  Write-Host "Use the full endpoint if it is a custom /chat API, for example: https://example.com/chat"
  Write-Host "Leave empty to keep the default Ollama configuration."
  $apiBaseUrl = (Read-Host "Colleague API URL").Trim()
  if (-not $apiBaseUrl) {
    Write-Host "No API URL entered. Keeping default LLM settings."
    return
  }

  $secureApiKey = Read-Host "API key" -AsSecureString
  $apiKey = Convert-SecureStringToPlainText $secureApiKey
  if (-not $apiKey) {
    throw "API URL was entered, but API key is empty."
  }

  $envPath = Join-Path $InstallDir ".env"
  Set-EnvValue $envPath "LLM_PROVIDER" "openai_compatible"
  Set-EnvValue $envPath "OPENAI_BASE_URL" $apiBaseUrl.TrimEnd("/")
  Set-EnvValue $envPath "OPENAI_API_KEY" $apiKey
  Set-EnvValue $envPath "LLM_MODEL" "colleague-api"
  Write-Host ".env has been configured for your colleague's API."
}

function Run-OfflineValidation {
  $venvPython = Join-Path $InstallDir ".venv\Scripts\python.exe"
  if (-not $SkipUnitTests) {
    Write-Step "Running unit tests"
    Invoke-CheckedCommand $venvPython @("-m", "unittest", "discover", "-v") "Unit tests failed."
  }

  Write-Step "Running no-WeChat simulation"
  Invoke-CheckedCommand $venvPython @("main.py", "simulate", "--reply", "ok, received", "hello") "No-WeChat simulation failed."
}

function New-ShortcutSafe([string]$ShortcutName, [string]$TargetPath) {
  if ($NoShortcuts) {
    return
  }
  try {
    $desktop = [Environment]::GetFolderPath("Desktop")
    if (-not $desktop) {
      return
    }
    $shortcutPath = Join-Path $desktop $ShortcutName
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $TargetPath
    $shortcut.WorkingDirectory = $InstallDir
    $shortcut.Save()
  } catch {
    Write-Host "Shortcut creation skipped: $($_.Exception.Message)" -ForegroundColor Yellow
  }
}

function Create-Shortcuts {
  Write-Step "Creating desktop shortcuts"
  New-ShortcutSafe "tqy-wechat-configure-api.lnk" (Join-Path $InstallDir "configure_api.bat")
  New-ShortcutSafe "tqy-wechat-install-compatible-wechat.lnk" (Join-Path $InstallDir "install_wechat_compatible.bat")
  New-ShortcutSafe "tqy-wechat-dry-run.lnk" (Join-Path $InstallDir "start_dry_run.bat")
  New-ShortcutSafe "tqy-wechat-live.lnk" (Join-Path $InstallDir "start_live.bat")
}

Start-Transcript -Path $InstallLog -Force | Out-Null
try {
  Write-Step "Starting installer"
  Write-Host "Package root: $PackageRoot"
  Write-Host "Install dir : $InstallDir"
  Write-Host "Install log : $InstallLog"

  Ensure-Windows
  $pythonCommand = Ensure-Python
  Install-WeChatIfRequested
  Copy-ProjectToInstallDir
  Ensure-Venv -PythonCommand $pythonCommand
  Install-PythonDependencies
  Initialize-EnvFile
  Configure-ColleagueApi
  Run-OfflineValidation
  Create-Shortcuts

  Write-Step "Environment is ready"
  Write-Host "Project path: $InstallDir"
  Write-Host ""
  Write-Host "Next steps:"
  Write-Host "  1. Open Windows desktop WeChat and log in."
  Write-Host "  2. Check .env. If you entered your colleague's API, LLM_PROVIDER should be openai_compatible."
  Write-Host "  3. If API config is wrong, run configure_api.bat."
  Write-Host "  4. If wxauto4 cannot attach to WeChat, run install_wechat_compatible.bat."
  Write-Host "  5. Run start_dry_run.bat first. Use start_live.bat only after dry-run looks correct."
  Write-Host ""
  Write-Host "Offline test command:"
  Write-Host "  .\.venv\Scripts\python.exe main.py simulate --reply `"ok, received`" `"hello`""
} catch {
  Write-Host ""
  Write-Host "INSTALL FAILED" -ForegroundColor Red
  Write-Host $_.Exception.Message -ForegroundColor Red
  Write-Host "Install log: $InstallLog"
  exit 1
} finally {
  try {
    Stop-Transcript | Out-Null
  } catch {
  }
}
