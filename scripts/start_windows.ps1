param(
  [string]$HostName = "0.0.0.0",
  [int]$Port = 5001,
  [switch]$NoBrowser,
  [switch]$SkipInstall,
  [switch]$RebuildFrontend
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..")).Path
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$VenvDir = Join-Path $ProjectRoot ".venv"
$RequirementsLock = Join-Path $ProjectRoot "requirements.lock"
$RequirementsTxt = Join-Path $ProjectRoot "requirements.txt"
$FrontendDist = Join-Path $ProjectRoot "frontend\dist"
$FrontendSource = Join-Path $ProjectRoot "frontend"

function Write-Step([string]$Message) {
  Write-Host ""
  Write-Host "==> $Message" -ForegroundColor Cyan
}

function Get-LocalAccessHost {
  if ($HostName -eq "0.0.0.0" -or $HostName -eq "::" -or $HostName -eq "*") {
    return "127.0.0.1"
  }
  return $HostName
}

function Get-LanAccessUrls([int]$TargetPort) {
  $urls = @()
  if ($HostName -ne "0.0.0.0" -and $HostName -ne "::" -and $HostName -ne "*") {
    return $urls
  }
  try {
    $addresses = [System.Net.Dns]::GetHostAddresses([System.Net.Dns]::GetHostName()) |
      Where-Object { $_.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -and -not $_.IPAddressToString.StartsWith("127.") } |
      Select-Object -ExpandProperty IPAddressToString -Unique
    foreach ($address in $addresses) {
      $urls += "http://$address`:$TargetPort/"
    }
  }
  catch {
    return $urls
  }
  return $urls
}

function Test-DataAgentHealth([int]$TargetPort) {
  try {
    $healthHost = Get-LocalAccessHost
    $response = Invoke-WebRequest -Uri "http://$healthHost`:$TargetPort/api/health" -UseBasicParsing -TimeoutSec 3
    return $response.StatusCode -eq 200 -and $response.Content -match '"ok"\s*:\s*true'
  }
  catch {
    return $false
  }
}

function Test-PortFree([string]$TargetHost, [int]$TargetPort) {
  $connectHost = if ($TargetHost -eq "0.0.0.0" -or $TargetHost -eq "::" -or $TargetHost -eq "*") {
    "127.0.0.1"
  } else {
    $TargetHost
  }
  $client = New-Object System.Net.Sockets.TcpClient
  try {
    $iar = $client.BeginConnect($connectHost, $TargetPort, $null, $null)
    $connected = $iar.AsyncWaitHandle.WaitOne(250, $false)
    if ($connected) {
      $client.EndConnect($iar)
      return $false
    }
    return $true
  }
  catch {
    return $true
  }
  finally {
    $client.Close()
  }
}

function Get-FreePort {
  $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
  try {
    $listener.Start()
    return [int]$listener.LocalEndpoint.Port
  }
  finally {
    $listener.Stop()
  }
}

function New-VirtualEnvironment {
  Write-Step "Creating Python virtual environment"
  $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
  if ($pyLauncher) {
    & $pyLauncher.Source -3 -m venv $VenvDir
    return
  }

  $python = Get-Command python -ErrorAction SilentlyContinue
  if ($python) {
    & $python.Source -m venv $VenvDir
    return
  }

  throw "Python was not found. Please install Python 3.10+ and run this script again."
}

function Test-RequiredPythonPackages {
  if (-not (Test-Path $VenvPython)) {
    return $false
  }
  & $VenvPython -c "import flask, waitress, pandas, sqlalchemy" *> $null
  return $LASTEXITCODE -eq 0
}

function Install-PythonDependencies {
  if ($SkipInstall) {
    Write-Host "SkipInstall is enabled; dependency installation is skipped." -ForegroundColor Yellow
    return
  }

  if (Test-RequiredPythonPackages) {
    Write-Host "Python dependencies are already available."
    return
  }

  Write-Step "Installing Python dependencies"
  if (Test-Path $RequirementsLock) {
    & $VenvPython -m pip install --require-hashes -r $RequirementsLock
  }
  elseif (Test-Path $RequirementsTxt) {
    & $VenvPython -m pip install -r $RequirementsTxt
  }
  else {
    throw "No requirements.lock or requirements.txt found."
  }
}

function Resolve-FrontendDir {
  $distIndex = Join-Path $FrontendDist "index.html"
  $sourceIndex = Join-Path $FrontendSource "index.html"

  if ($RebuildFrontend -or -not (Test-Path $distIndex)) {
    $npm = Get-Command npm -ErrorAction SilentlyContinue
    if ($npm) {
      Write-Step "Building frontend assets"
      Push-Location $ProjectRoot
      try {
        & $npm.Source run build
        if ($LASTEXITCODE -ne 0) {
          throw "npm run build failed."
        }
      }
      finally {
        Pop-Location
      }
    }
    elseif (-not (Test-Path $sourceIndex)) {
      throw "frontend/dist/index.html does not exist and npm is not available to build it."
    }
    else {
      Write-Host "npm was not found; using frontend source directory for local startup." -ForegroundColor Yellow
    }
  }

  if (Test-Path $distIndex) {
    return (Resolve-Path $FrontendDist).Path
  }
  if (Test-Path $sourceIndex) {
    return (Resolve-Path $FrontendSource).Path
  }
  throw "No usable frontend index.html found."
}

Set-Location $ProjectRoot

if (Test-DataAgentHealth $Port) {
  $runningHost = Get-LocalAccessHost
  $runningUrl = "http://$runningHost`:$Port/"
  Write-Host "DataAgent is already running: $runningUrl" -ForegroundColor Green
  if (-not $NoBrowser) {
    Start-Process $runningUrl
  }
  exit 0
}

if (-not (Test-PortFree $HostName $Port)) {
  $oldPort = $Port
  $Port = Get-FreePort
  Write-Host "Port $oldPort is occupied. DataAgent will use port $Port instead." -ForegroundColor Yellow
}

if (-not (Test-Path $VenvPython)) {
  New-VirtualEnvironment
}

Install-PythonDependencies
$ResolvedFrontendDir = Resolve-FrontendDir

$env:MERIDIAN_ENV = if ($env:MERIDIAN_ENV) { $env:MERIDIAN_ENV } else { "development" }
$env:MERIDIAN_HOST = $HostName
$env:MERIDIAN_PORT = [string]$Port
$env:MERIDIAN_DEBUG = if ($env:MERIDIAN_DEBUG) { $env:MERIDIAN_DEBUG } else { "0" }
$env:MERIDIAN_FRONTEND_DIR = $ResolvedFrontendDir
$env:MERIDIAN_ALLOWED_ORIGINS = if ($env:MERIDIAN_ALLOWED_ORIGINS) {
  $env:MERIDIAN_ALLOWED_ORIGINS
} else {
  "http://$HostName`:$Port,http://localhost:$Port,http://127.0.0.1:$Port"
}
$env:MERIDIAN_TRUSTED_HOSTS = if ($env:MERIDIAN_TRUSTED_HOSTS) {
  $env:MERIDIAN_TRUSTED_HOSTS
} elseif ($HostName -eq "0.0.0.0" -or $HostName -eq "::" -or $HostName -eq "*") {
  ""
} else {
  "$HostName,localhost,127.0.0.1"
}

$AccessHost = Get-LocalAccessHost
$Url = "http://$AccessHost`:$Port/"
$LanUrls = Get-LanAccessUrls $Port
Write-Step "Starting DataAgent"
Write-Host "Project root : $ProjectRoot"
Write-Host "Frontend dir : $ResolvedFrontendDir"
Write-Host "Listen host  : $HostName"
Write-Host "Access URL   : $Url"
if ($LanUrls.Count -gt 0) {
  Write-Host "LAN URLs     : $($LanUrls -join ', ')"
}
Write-Host "Press Ctrl+C in this window to stop the service."

if (-not $NoBrowser) {
  Start-Job -ScriptBlock {
    param([string]$TargetUrl)
    Start-Sleep -Seconds 2
    Start-Process $TargetUrl
  } -ArgumentList $Url | Out-Null
}

& $VenvPython (Join-Path $ProjectRoot "app.py")
