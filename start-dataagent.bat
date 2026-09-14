@echo off
setlocal

set "PROJECT_ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_ROOT%scripts\start_windows.ps1" %*

set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
  echo.
  echo DataAgent startup failed. Error code: %EXIT_CODE%
  pause
)
exit /b %EXIT_CODE%
