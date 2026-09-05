$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$BuildRoot = Join-Path $ProjectRoot "build\desktop"
if (Test-Path -LiteralPath $BuildRoot) {
    Remove-Item -LiteralPath $BuildRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $BuildRoot | Out-Null
python (Join-Path $ProjectRoot "packaging\build_staging.py") `
    --source $ProjectRoot --destination (Join-Path $BuildRoot "staging")
if ($LASTEXITCODE -ne 0) { throw "Packaging staging failed." }
$env:MERIDIAN_STAGING_ROOT = Join-Path $BuildRoot "staging"
python -m PyInstaller --clean --noconfirm `
    --workpath (Join-Path $BuildRoot "work") `
    --distpath (Join-Path $BuildRoot "dist") `
    (Join-Path $ProjectRoot "packaging\meridian.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }
Write-Host "Desktop package: $(Join-Path $BuildRoot 'dist')"
