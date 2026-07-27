param(
  [string]$DownloadUrl = "https://github.com/cscnk52/wechat-windows-versions/releases/download/v4.1.8.107/weixin_4.1.8.107.exe",
  [string]$ExpectedSha256 = "b79aa3fdfd208d64f38fcc354684f9af213c05993c6f603521f2813930362b90"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0
Set-Location -LiteralPath $PSScriptRoot

function Write-Step([string]$Message) {
  Write-Host ""
  Write-Host "==> $Message" -ForegroundColor Cyan
}

function Get-WeChatProcesses {
  @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ProcessName -match "^(WeChat|Weixin|WXWork)$"
  })
}

Write-Step "Install compatible Windows WeChat"
Write-Host "This script downloads Windows WeChat 4.1.8.107 from the version archive:"
Write-Host "  $DownloadUrl"
Write-Host ""
Write-Host "Target compatible version family: 4.1.8, used for wxauto4 attachment in this project."
Write-Host "The original Tencent CDN URL for this archived version is no longer reliably available."
Write-Host "The download is checked against the recorded SHA256 before installation."
Write-Host "If Windows says a newer WeChat is already installed, uninstall the current WeChat manually, then run this script again."
Write-Host ""

$confirmRaw = Read-Host "Type YES or Y to download and run the compatible WeChat installer"
if ($null -eq $confirmRaw) {
  $confirmRaw = ""
}
$confirm = $confirmRaw.Trim().ToUpperInvariant()
if (@("YES", "Y") -notcontains $confirm) {
  Write-Host "Canceled."
  exit 0
}

$running = Get-WeChatProcesses
if ($running.Count -gt 0) {
  Write-Host ""
  Write-Host "WeChat appears to be running. Please quit WeChat completely first." -ForegroundColor Yellow
  $running | Select-Object ProcessName, Id | Format-Table | Out-String | Write-Host
  Read-Host "After quitting WeChat, press Enter to continue"
}

$downloadDir = Join-Path $env:TEMP "tqy-wechat-compatible"
New-Item -ItemType Directory -Force -Path $downloadDir | Out-Null
$installerPath = Join-Path $downloadDir "weixin_4.1.8.107.exe"

Write-Step "Downloading compatible WeChat installer"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Write-Host "The file is about 233 MB. On some networks this can look paused for a few minutes."
try {
  Invoke-WebRequest -Uri $DownloadUrl -OutFile $installerPath -UseBasicParsing
} catch {
  Write-Host ""
  Write-Host "Automatic download failed:" -ForegroundColor Yellow
  Write-Host $_.Exception.Message -ForegroundColor Yellow
  Write-Host ""
  Write-Host "Opening the download page in your browser. If it downloads successfully, drag the downloaded .exe file into this window."
  Write-Host "Download URL:"
  Write-Host "  $DownloadUrl"
  try {
    Start-Process $DownloadUrl
  } catch {
    Write-Host "Could not open browser automatically: $($_.Exception.Message)" -ForegroundColor Yellow
  }

  $manualPath = Read-Host "Downloaded .exe path, or press Enter to cancel"
  if ($null -eq $manualPath -or $manualPath.Trim() -eq "") {
    throw "WeChat installer download was not completed."
  }
  $manualPath = $manualPath.Trim().Trim('"')
  if (-not (Test-Path -LiteralPath $manualPath)) {
    throw "Manual installer path does not exist: $manualPath"
  }
  Copy-Item -LiteralPath $manualPath -Destination $installerPath -Force
}

$installer = Get-Item -LiteralPath $installerPath
if ($installer.Length -lt 100000000) {
  throw "Downloaded installer is unexpectedly small: $($installer.Length) bytes"
}

$hash = Get-FileHash -LiteralPath $installerPath -Algorithm SHA256
if ($hash.Hash.ToLowerInvariant() -ne $ExpectedSha256.ToLowerInvariant()) {
  throw "Downloaded installer hash mismatch. Expected $ExpectedSha256 but got $($hash.Hash)."
}

Write-Host "Downloaded: $installerPath"
Write-Host "Size      : $($installer.Length) bytes"
Write-Host "SHA256    : $($hash.Hash)"

Write-Step "Running WeChat installer"
Write-Host "Complete the WeChat installer UI, then log in and keep the main chat window visible."
Start-Process -FilePath $installerPath -Wait

Write-Step "Next"
Write-Host "1. Open Windows WeChat and log in."
Write-Host "2. Keep the main chat window visible."
Write-Host "3. Run start_dry_run.bat from:"
Write-Host "   $PSScriptRoot"
