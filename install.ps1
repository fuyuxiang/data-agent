$ErrorActionPreference = "Stop"

$Repository = if ($env:MERIDIAN_REPOSITORY) { $env:MERIDIAN_REPOSITORY } else { "https://github.com/fuyuxiang/data-agent.git" }
$InstallRoot = if ($env:MERIDIAN_INSTALL_ROOT) { $env:MERIDIAN_INSTALL_ROOT } else { Join-Path $env:LOCALAPPDATA "MeridianAnalytics" }
$Project = Join-Path $InstallRoot "data-agent"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "Git is required." }
if (Get-Command py -ErrorAction SilentlyContinue) {
    $PythonExe = "py"
    $PythonArgs = @("-3")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $PythonExe = "python"
    $PythonArgs = @()
} else {
    throw "Python 3.10+ is required."
}
& $PythonExe @PythonArgs -c "import sys;raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Python 3.10+ is required." }

New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
if (Test-Path (Join-Path $Project ".git")) {
    git -C $Project pull --ff-only
} elseif (Test-Path $Project) {
    throw "$Project exists but is not a Git checkout."
} else {
    git clone --depth 1 $Repository $Project
}

& $PythonExe @PythonArgs -m venv (Join-Path $Project ".venv")
$VenvPython = Join-Path $Project ".venv\Scripts\python.exe"
& $VenvPython -m pip install --disable-pip-version-check --require-hashes -r (Join-Path $Project "requirements.lock")

$Launcher = Join-Path $env:USERPROFILE "meridian-analytics.bat"
@"
@echo off
cd /d "$Project"
".venv\Scripts\python.exe" app.py %*
"@ | Set-Content -Encoding ASCII $Launcher
Write-Host "Installed. Run: $Launcher"
