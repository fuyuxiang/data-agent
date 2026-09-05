@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "MERIDIAN_PYTHON="
where py >nul 2>&1 && set "MERIDIAN_PYTHON=py -3"
if not defined MERIDIAN_PYTHON where python >nul 2>&1 && set "MERIDIAN_PYTHON=python"
if not defined MERIDIAN_PYTHON (
  echo [ERROR] Python 3.10+ is required.
  pause
  exit /b 1
)

%MERIDIAN_PYTHON% -c "import sys;raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
  echo [ERROR] Python 3.10+ is required.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" %MERIDIAN_PYTHON% -m venv .venv
if errorlevel 1 exit /b 1

".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --require-hashes -r requirements.lock
if errorlevel 1 (
  echo [ERROR] Dependency installation failed.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" app.py
set "MERIDIAN_EXIT=%ERRORLEVEL%"
if not "%MERIDIAN_EXIT%"=="0" pause
exit /b %MERIDIAN_EXIT%
