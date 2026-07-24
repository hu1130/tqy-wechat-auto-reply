@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_wechat_compatible.ps1"
set EXIT_CODE=%ERRORLEVEL%

echo.
if "%EXIT_CODE%"=="0" (
  echo Compatible WeChat installer finished.
) else (
  echo Compatible WeChat installer failed.
)
pause
exit /b %EXIT_CODE%
