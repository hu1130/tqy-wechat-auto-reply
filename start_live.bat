@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv. Run setup_windows.ps1 first.
  pause
  exit /b 1
)

echo This will send real replies in WeChat.
set /p CONFIRM=Type YES to continue: 
if /i not "%CONFIRM%"=="YES" (
  echo Canceled.
  pause
  exit /b 0
)

".venv\Scripts\python.exe" main.py doctor
if errorlevel 1 (
  echo.
  echo Doctor failed. Fix the items above before starting live mode.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" main.py run --live
pause
