[CmdletBinding()]
param(
    [int]$Port = 8080,
    [string]$DataDir = (Join-Path $env:LOCALAPPDATA "miot-panel-data"),
    [switch]$Kiosk,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$InstallDir = $PSScriptRoot
$python = Join-Path $InstallDir ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Python environment missing. Run windows\install.ps1 first."
}
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$env:MIOT_ROOT = $DataDir
$env:MIOT_APP_DIR = $InstallDir
$env:MIOT_PORT = "$Port"
$env:MIOT_LOG_FILE = Join-Path $DataDir "miot-cloud.log"

function Test-Panel {
    try {
        Invoke-WebRequest "http://127.0.0.1:$Port/api/status" -UseBasicParsing -TimeoutSec 2 | Out-Null
        return $true
    } catch {
        return $false
    }
}

if (-not (Test-Panel)) {
    Write-Host "== starting MIOT Panel on port $Port"
    $proc = Start-Process -FilePath $python -ArgumentList @("`"$InstallDir\miot-cloud.py`"") -WorkingDirectory $InstallDir -WindowStyle Hidden -PassThru
    $ok = $false
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-Panel) { $ok = $true; break }
        if ($proc.HasExited) { break }
    }
    if (-not $ok) {
        throw "Server did not start. Check $env:MIOT_LOG_FILE"
    }
}

if ($NoBrowser) { return }
$url = "http://127.0.0.1:$Port/"
$edge = @(
    (Join-Path ${env:ProgramFiles(x86)} "Microsoft\Edge\Application\msedge.exe"),
    (Join-Path $env:ProgramFiles "Microsoft\Edge\Application\msedge.exe")
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
$chrome = @(
    (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe")
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

$browser = if ($edge) { $edge } elseif ($chrome) { $chrome } else { $null }
if (-not $browser) {
    Start-Process $url
    return
}
if ($Kiosk) {
    if ($browser -like "*msedge.exe") {
        Start-Process $browser -ArgumentList @("--kiosk", $url, "--edge-kiosk-type=fullscreen", "--no-first-run")
    } else {
        Start-Process $browser -ArgumentList @("--kiosk", $url, "--no-first-run")
    }
} else {
    Start-Process $browser -ArgumentList @("--app=$url", "--no-first-run")
}
