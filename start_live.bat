@echo off
if /i not "%~1"=="__inner" (
  start "tqy-wechat live" cmd /k ""%~f0" __inner"
  exit /b
)
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

echo == tqy-wechat-auto-reply live ==
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

echo This will send real replies in WeChat.
set /p "CONFIRM=Type YES to continue: "
if /i not "%CONFIRM%"=="YES" (
  echo Canceled.
  exit /b 0
)

echo Checking environment...
".venv\Scripts\python.exe" main.py doctor
if errorlevel 1 (
  echo.
  echo Doctor failed. Fix the items above before starting live mode.
  exit /b 1
)

echo.
echo Starting live mode. Press Ctrl+C to stop.
echo.
".venv\Scripts\python.exe" main.py run --live
echo.
echo Bot exited with code %ERRORLEVEL%.
exit /b %ERRORLEVEL%
