param(
  [string]$Version = "1.0.0",
  [switch]$SkipInstaller,
  [switch]$NoClean
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BuildRoot = Join-Path $ProjectRoot "build\windows"
$DistRoot = Join-Path $ProjectRoot "dist\windows"
$AppDist = Join-Path $DistRoot "app"
$AppBundle = Join-Path $AppDist "DataAgent"
$InstallerPath = Join-Path $DistRoot "DataAgent-Setup-$Version.exe"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

function Assert-InProject([string]$PathToCheck) {
  $resolved = [System.IO.Path]::GetFullPath($PathToCheck)
  if (-not $resolved.StartsWith($ProjectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to write outside project: $resolved"
  }
}

if (-not (Test-Path $Python)) {
  throw "Virtual environment Python not found: $Python"
}

Push-Location $ProjectRoot
try {
  Write-Host "==> Building frontend"
  npm run build

  Write-Host "==> Checking PyInstaller"
  & $Python -m PyInstaller --version | Out-Null

  if (-not $NoClean) {
    Assert-InProject $BuildRoot
    Assert-InProject $DistRoot
    if (Test-Path $BuildRoot) { Remove-Item -LiteralPath $BuildRoot -Recurse -Force }
    if (Test-Path $DistRoot) { Remove-Item -LiteralPath $DistRoot -Recurse -Force }
  }
  New-Item -ItemType Directory -Force -Path $BuildRoot, $AppDist | Out-Null

  $pyinstallerArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--name", "DataAgent",
    "--console",
    "--onedir",
    "--distpath", $AppDist,
    "--workpath", (Join-Path $BuildRoot "pyinstaller-work"),
    "--specpath", (Join-Path $BuildRoot "spec"),
    "--add-data", "$ProjectRoot\frontend\dist;frontend\dist",
    "--add-data", "$ProjectRoot\backend\analysis_modules;backend\analysis_modules",
    "--add-data", "$ProjectRoot\backend\document_output\PPT\PPT_template;backend\document_output\PPT\PPT_template",
    "--add-data", "$ProjectRoot\deploy\samples;deploy\samples",
    "--add-data", "$ProjectRoot\skills;skills",
    "--collect-submodules", "backend",
    "--collect-data", "pyecharts",
    "--collect-data", "plotly",
    "--collect-data", "statsmodels",
    "--exclude-module", "pytest",
    "--exclude-module", "tkinter",
    "--exclude-module", "pyarrow.tests",
    "--exclude-module", "sklearn.tests",
    "--exclude-module", "scipy.tests",
    "--exclude-module", "matplotlib.tests",
    "--exclude-module", "pandas.tests",
    "--exclude-module", "plotly.tests",
    "packaging\windows\dataagent_desktop.py"
  )

  Write-Host "==> Building DataAgent.exe"
  & $Python @pyinstallerArgs

  $AppExe = Join-Path $AppBundle "DataAgent.exe"
  if (-not (Test-Path $AppExe)) {
    throw "PyInstaller did not produce $AppExe"
  }

  if (-not $SkipInstaller) {
    $AppZip = Join-Path $BuildRoot "DataAgent-app.zip"
    if (Test-Path $AppZip) { Remove-Item -LiteralPath $AppZip -Force }
    Write-Host "==> Compressing installed application bundle"
    Compress-Archive -Path (Join-Path $AppBundle "*") -DestinationPath $AppZip -CompressionLevel Optimal

    Write-Host "==> Building native Windows installer"
    $NativeInstallerRoot = Join-Path $BuildRoot "native-installer"
    New-Item -ItemType Directory -Force -Path $NativeInstallerRoot | Out-Null

    $InstallerStub = Join-Path $NativeInstallerRoot "DataAgent-Setup-$Version.stub.exe"
    $CscCandidates = @(
      (Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"),
      (Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe")
    )
    $Csc = $CscCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $Csc) {
      throw 'Windows .NET CSharp compiler csc.exe was not found; cannot build installer.'
    }

    & $Csc `
      /nologo `
      /target:exe `
      /out:$InstallerStub `
      /reference:System.IO.Compression.dll `
      /reference:System.IO.Compression.FileSystem.dll `
      /reference:Microsoft.CSharp.dll `
      packaging\windows\DataAgentInstaller.cs
    if ($LASTEXITCODE -ne 0) {
      throw 'CSharp installer stub compilation failed.'
    }

    $MarkerBytes = [System.Text.Encoding]::ASCII.GetBytes("__DATAAGENT_APP_ZIP_V1__")
    if (Test-Path $InstallerPath) { Remove-Item -LiteralPath $InstallerPath -Force }
    Copy-Item -LiteralPath $InstallerStub -Destination $InstallerPath -Force
    $InstallerStream = [System.IO.File]::Open($InstallerPath, [System.IO.FileMode]::Append, [System.IO.FileAccess]::Write)
    try {
      $InstallerStream.Write($MarkerBytes, 0, $MarkerBytes.Length)
      $ZipStream = [System.IO.File]::OpenRead($AppZip)
      try {
        $ZipStream.CopyTo($InstallerStream)
      }
      finally {
        $ZipStream.Dispose()
      }
    }
    finally {
      $InstallerStream.Dispose()
    }

    if (-not (Test-Path $InstallerPath)) {
      throw "Installer was not produced: $InstallerPath"
    }
  }

  Write-Host ""
  Write-Host "Build completed."
  Write-Host "Portable app: $AppExe"
  if (-not $SkipInstaller) {
    Write-Host "Installer:     $InstallerPath"
  }
}
finally {
  Pop-Location
}
