@echo off
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_one_click.ps1" -ConfigureApi
set EXIT_CODE=%ERRORLEVEL%

echo.
if "%EXIT_CODE%"=="0" (
  echo Install finished.
) else (
  echo Install failed. Check the log path printed above.
)
pause
exit /b %EXIT_CODE%
