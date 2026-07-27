@echo off
if /i not "%~1"=="__inner" (
  start "tqy-wechat dry-run" cmd /k ""%~f0" __inner"
  exit /b
)
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

echo == tqy-wechat-auto-reply dry-run ==
echo Project path: %CD%
echo.

if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv. Run setup_windows.ps1 first.
  exit /b 1
)

if not exist ".env" (
  echo Missing .env. Run install_one_click.bat first.
  exit /b 1
)

echo Checking environment...
".venv\Scripts\python.exe" main.py doctor
if errorlevel 1 (
  echo.
  echo Doctor failed. Fix the items above before starting the bot.
  exit /b 1
)

echo.
echo Starting dry-run. This will NOT send real WeChat messages.
echo Press Ctrl+C to stop.
echo.
".venv\Scripts\python.exe" main.py run --dry-run
echo.
echo Bot exited with code %ERRORLEVEL%.
exit /b %ERRORLEVEL%
