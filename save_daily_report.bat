@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv. Run setup_windows.ps1 first.
  exit /b 1
)

".venv\Scripts\python.exe" main.py admin report --save
exit /b %errorlevel%
