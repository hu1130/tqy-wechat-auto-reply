@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0configure_api.ps1"
set EXIT_CODE=%ERRORLEVEL%

echo.
if "%EXIT_CODE%"=="0" (
  echo API config saved.
) else (
  echo API config failed.
)
pause
exit /b %EXIT_CODE%
