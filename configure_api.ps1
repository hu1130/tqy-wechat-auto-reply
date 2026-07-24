param(
  [string]$EnvPath = ".env"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0
Set-Location -LiteralPath $PSScriptRoot

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

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
  throw "Missing .venv. Run install_one_click.bat first."
}

if (-not (Test-Path -LiteralPath $EnvPath)) {
  & ".\.venv\Scripts\python.exe" main.py init --yes
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to create .env."
  }
}

Write-Host "== Configure colleague/boss API =="
Write-Host ""
Write-Host "Paste the real API URL and key. Do not enter placeholder text."
Write-Host "If the endpoint is the custom session API, use the full /chat URL."
Write-Host "Example: https://example.com/chat"
Write-Host ""

$apiBaseUrl = (Read-Host "OPENAI_BASE_URL").Trim()
if (-not $apiBaseUrl) {
  throw "OPENAI_BASE_URL is empty."
}
if ($apiBaseUrl -notmatch "^https?://") {
  throw "OPENAI_BASE_URL must start with http:// or https://"
}

$secureApiKey = Read-Host "OPENAI_API_KEY" -AsSecureString
$apiKey = Convert-SecureStringToPlainText $secureApiKey
if (-not $apiKey) {
  throw "OPENAI_API_KEY is empty."
}

Set-EnvValue $EnvPath "LLM_PROVIDER" "openai_compatible"
Set-EnvValue $EnvPath "OPENAI_BASE_URL" $apiBaseUrl.TrimEnd("/")
Set-EnvValue $EnvPath "OPENAI_API_KEY" $apiKey
Set-EnvValue $EnvPath "LLM_MODEL" "colleague-api"

Write-Host ""
Write-Host "Saved to $EnvPath"
Write-Host "Current public config:"
& ".\.venv\Scripts\python.exe" main.py config
if ($LASTEXITCODE -ne 0) {
  throw "Config check failed."
}
