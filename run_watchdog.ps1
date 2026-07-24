param(
  [switch]$Live,
  [ValidateSet("local", "center")]
  [string]$Mode = "local",
  [string]$Env = ".env",
  [string]$PythonPath = "",
  [string]$Account = "",
  [int]$RestartDelaySeconds = 10,
  [int]$MaxRestartsPerHour = 12,
  [int]$HealthTimeoutSeconds = 600
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$python = if ($PythonPath.Trim()) { $PythonPath } else { Join-Path $PSScriptRoot ".venv\Scripts\python.exe" }
if (-not (Test-Path -LiteralPath $python)) {
  throw "Python runtime not found: $python"
}

$envPath = if ([System.IO.Path]::IsPathRooted($Env)) { $Env } else { Join-Path $PSScriptRoot $Env }
if (-not (Test-Path -LiteralPath $envPath)) {
  throw "Env file not found: $envPath"
}
if ($Account.Trim()) {
  $env:ACCOUNT_ID = $Account.Trim()
}

$runMode = if ($Live) { "--live" } else { "--dry-run" }
$restartTimes = New-Object System.Collections.Generic.List[datetime]

while ($true) {
  if ($Mode -eq "center") {
    & $python main.py --env $envPath connector doctor --channel wechat_desktop
  } else {
    & $python main.py --env $envPath doctor --skip-llm
  }
  if ($LASTEXITCODE -ne 0) {
    Write-Host "Doctor failed. Not starting bot."
    exit $LASTEXITCODE
  }

  $configJson = & $python main.py --env $envPath config
  if ($LASTEXITCODE -ne 0) {
    Write-Host "Config load failed. Not starting bot."
    exit $LASTEXITCODE
  }
  $config = $configJson | ConvertFrom-Json
  $healthFile = if ([System.IO.Path]::IsPathRooted([string]$config.health_file)) {
    [string]$config.health_file
  } else {
    Join-Path $PSScriptRoot ([string]$config.health_file)
  }
  $healthDir = Split-Path -Parent $healthFile
  if ($healthDir) {
    New-Item -ItemType Directory -Force -Path $healthDir | Out-Null
  }

  $now = Get-Date
  $restartTimes.Add($now)
  $cutoff = $now.AddHours(-1)
  $recentRestarts = @($restartTimes | Where-Object { $_ -ge $cutoff })
  $restartTimes.Clear()
  foreach ($item in $recentRestarts) {
    $restartTimes.Add($item)
  }
  if ($restartTimes.Count -gt $MaxRestartsPerHour) {
    Write-Host "Too many restarts in one hour. Stop watchdog."
    exit 2
  }

  $accountLabel = if ($config.account_id) { " account=$($config.account_id)" } else { "" }
  $displayMode = if ($Mode -eq "center") { "center Agent" } else { "bot $runMode" }
  Write-Host "Starting $displayMode$accountLabel..."
  if (Test-Path -LiteralPath $healthFile) {
    Remove-Item -LiteralPath $healthFile -Force -ErrorAction SilentlyContinue
  }
  $processArguments = if ($Mode -eq "center") {
    @("main.py", "--env", $envPath, "connector", "run-center", "--channel", "wechat_desktop")
  } else {
    @("main.py", "--env", $envPath, "run", $runMode)
  }
  $process = Start-Process -FilePath $python -ArgumentList $processArguments -WorkingDirectory $PSScriptRoot -PassThru -WindowStyle Hidden
  $startedAt = Get-Date
  while (-not $process.HasExited) {
    Start-Sleep -Seconds 15
    if (-not (Test-Path -LiteralPath $healthFile)) {
      $startupAgeSeconds = ((Get-Date) - $startedAt).TotalSeconds
      if ($startupAgeSeconds -le $HealthTimeoutSeconds) {
        continue
      }
      Write-Host "Health file was not created after $([int]$startupAgeSeconds)s. Restarting bot process $($process.Id)."
      Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
      break
    }
    $lastWrite = (Get-Item -LiteralPath $healthFile).LastWriteTime
    if ($lastWrite -lt $startedAt) {
      continue
    }
    try {
      $health = Get-Content -LiteralPath $healthFile -Raw | ConvertFrom-Json
      if ([string]$health.status -eq "blocked") {
        Write-Host "Health status is blocked. Restarting bot process $($process.Id)."
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        break
      }
    } catch {
      Write-Host "Health file could not be parsed yet: $($_.Exception.Message)"
    }
    $ageSeconds = ((Get-Date) - $lastWrite).TotalSeconds
    if ($ageSeconds -le $HealthTimeoutSeconds) {
      continue
    }
    Write-Host "Health file is stale for $([int]$ageSeconds)s. Restarting bot process $($process.Id)."
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    break
  }
  $process.WaitForExit()
  $exitCode = $process.ExitCode
  Write-Host "Bot exited with code $exitCode. Restarting in $RestartDelaySeconds seconds."
  Start-Sleep -Seconds $RestartDelaySeconds
}
