param(
  [switch]$Live,
  [int]$RestartDelaySeconds = 10,
  [int]$MaxRestartsPerHour = 12
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
  throw "Missing .venv. Run setup_windows.ps1 first."
}

$mode = if ($Live) { "--live" } else { "--dry-run" }
$restartTimes = New-Object System.Collections.Generic.List[datetime]

while ($true) {
  & $python main.py doctor --skip-llm
  if ($LASTEXITCODE -ne 0) {
    Write-Host "Doctor failed. Not starting bot."
    exit $LASTEXITCODE
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

  Write-Host "Starting bot in $mode mode..."
  & $python main.py run $mode
  $exitCode = $LASTEXITCODE
  Write-Host "Bot exited with code $exitCode. Restarting in $RestartDelaySeconds seconds."
  Start-Sleep -Seconds $RestartDelaySeconds
}
