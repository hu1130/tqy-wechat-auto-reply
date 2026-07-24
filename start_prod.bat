@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv. Run setup_windows.ps1 first.
  pause
  exit /b 1
)

echo Production mode will keep the bot running under watchdog.
echo Make sure .env uses a small whitelist and REQUIRE_MANUAL_CONFIRM=1 before live use.
set /p "CONFIRM=Type YES to start production watchdog: "
if /i not "%CONFIRM%"=="YES" (
  echo Canceled.
  pause
  exit /b 0
)

powershell -ExecutionPolicy Bypass -File "%~dp0run_watchdog.ps1" -Live
pause
